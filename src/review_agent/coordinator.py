import asyncio
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from review_agent.attempt import AttemptOutcome, AttemptPublication, AttemptStatus
from review_agent.github import (
    CheckRun,
    CheckRunOutputKind,
    CheckRunStatus,
    GitHubError,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.models import ReviewRequest
from review_agent.process_manager import (
    AttemptExecution,
    AttemptLaunchError,
    SubmissionOutcome,
)
from review_agent.reconciliation import DesiredCheckRun

_MAX_CONCURRENT_REVIEWS = 10


class CheckRunGateway(Protocol):
    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]: ...

    def create_check_run(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> CheckRun: ...

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool: ...


class AttemptLauncher(Protocol):
    async def launch(
        self,
        request: ReviewRequest,
        *,
        check_run_id: int,
    ) -> AttemptExecution: ...


class DesiredStateReconciler(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def set_desired(self, desired: DesiredCheckRun) -> None: ...


class _Lifecycle(Enum):
    CREATED = auto()
    ACCEPTING = auto()
    STOPPING = auto()
    STOPPED = auto()


class ReviewAttemptCoordinator:
    """Own durable admission and the complete parent-side Check Run lifecycle."""

    def __init__(
        self,
        *,
        github: CheckRunGateway,
        process: AttemptLauncher,
        reconciler: DesiredStateReconciler,
        max_concurrent_reviews: int = 1,
    ) -> None:
        if (
            isinstance(max_concurrent_reviews, bool)
            or not isinstance(max_concurrent_reviews, int)
            or not 1 <= max_concurrent_reviews <= _MAX_CONCURRENT_REVIEWS
        ):
            message = "maximum concurrent reviews must be between one and ten"
            raise ValueError(message)
        self._github = github
        self._process = process
        self._reconciler = reconciler
        self._max_concurrent_reviews = max_concurrent_reviews
        self._lifecycle = _Lifecycle.CREATED
        self._admission_lock = asyncio.Lock()
        self._reserved = 0
        self._attempt_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        if self._lifecycle is not _Lifecycle.CREATED:
            message = "review attempt coordinator cannot be restarted"
            raise RuntimeError(message)
        await self._reconciler.__aenter__()
        self._lifecycle = _Lifecycle.ACCEPTING
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        async with self._admission_lock:
            self._lifecycle = _Lifecycle.STOPPING
            tasks = tuple(self._attempt_tasks)
        try:
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            try:
                await self._reconciler.__aexit__(exc_type, exc_value, traceback)
            finally:
                self._lifecycle = _Lifecycle.STOPPED

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:  # noqa: PLR0911
        async with self._admission_lock:
            if self._lifecycle is not _Lifecycle.ACCEPTING:
                return SubmissionOutcome.STOPPING

            identity = derive_review_identity(request)
            try:
                check_runs = await asyncio.to_thread(
                    self._github.list_check_runs,
                    identity=identity,
                    installation_id=request.installation_id,
                )
            except GitHubError:
                return SubmissionOutcome.UNAVAILABLE

            owned = tuple(
                check_run
                for check_run in check_runs
                if self._github.is_owned_check_run(check_run, identity=identity)
            )
            if any(check_run.status is not CheckRunStatus.COMPLETED for check_run in owned):
                return SubmissionOutcome.ALREADY_RUNNING
            if owned:
                return SubmissionOutcome.ALREADY_REVIEWED
            if self._reserved >= self._max_concurrent_reviews:
                return SubmissionOutcome.AT_CAPACITY

            self._reserved += 1
            try:
                check_run = await asyncio.to_thread(
                    self._github.create_check_run,
                    identity=identity,
                    installation_id=request.installation_id,
                )
            except GitHubError:
                self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE

            try:
                execution = await self._process.launch(
                    request,
                    check_run_id=check_run.id,
                )
            except AttemptLaunchError as error:
                try:
                    await self._reconciler.set_desired(
                        _terminal_desired(
                            check_run_id=check_run.id,
                            identity=identity,
                            outcome=error.outcome,
                        )
                    )
                finally:
                    self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE

            await self._reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=check_run.id,
                    identity=identity,
                    attempt_id=execution.attempt_id,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )
            task = asyncio.create_task(
                self._complete_attempt(
                    execution,
                    check_run_id=check_run.id,
                    identity=identity,
                )
            )
            self._attempt_tasks.add(task)
            task.add_done_callback(self._attempt_tasks.discard)
            return SubmissionOutcome.ACCEPTED

    async def _complete_attempt(
        self,
        execution: AttemptExecution,
        *,
        check_run_id: int,
        identity: ReviewIdentity,
    ) -> None:
        try:
            outcome = await execution.wait()
            await self._reconciler.set_desired(
                _terminal_desired(
                    check_run_id=check_run_id,
                    identity=identity,
                    outcome=outcome,
                )
            )
        finally:
            async with self._admission_lock:
                self._reserved -= 1


def _terminal_desired(
    *,
    check_run_id: int,
    identity: ReviewIdentity,
    outcome: AttemptOutcome,
) -> DesiredCheckRun:
    if outcome.publication is AttemptPublication.PUBLISHED and outcome.review_status is not None:
        output_kind = (
            CheckRunOutputKind.CLEAN
            if outcome.review_status == "no_important_issues"
            else CheckRunOutputKind.FINDINGS
        )
        return DesiredCheckRun(
            check_run_id=check_run_id,
            identity=identity,
            attempt_id=outcome.attempt_id,
            output_kind=output_kind,
            finding_count=0 if output_kind is CheckRunOutputKind.CLEAN else None,
        )
    if outcome.status is AttemptStatus.TIMED_OUT:
        output_kind = CheckRunOutputKind.TIMEOUT
    elif outcome.publication is AttemptPublication.UNKNOWN:
        output_kind = CheckRunOutputKind.PUBLICATION_UNKNOWN
    else:
        output_kind = CheckRunOutputKind.TECHNICAL_FAILURE
    return DesiredCheckRun(
        check_run_id=check_run_id,
        identity=identity,
        attempt_id=outcome.attempt_id,
        output_kind=output_kind,
        failure_stage=(
            outcome.failure_stage
            if output_kind is CheckRunOutputKind.TECHNICAL_FAILURE
            else None
        ),
        failure_category=(
            outcome.failure_category.value
            if output_kind is CheckRunOutputKind.TECHNICAL_FAILURE
            and outcome.failure_category is not None
            else None
        ),
    )
