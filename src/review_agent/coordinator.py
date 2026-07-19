import asyncio
import uuid
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

from review_agent.active_attempts import (
    ActiveAttempt,
    ActiveAttemptRegistry,
    ActiveAttemptStateError,
    VolatileActiveAttemptRegistry,
)
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
from review_agent.reconciliation import DesiredCheckRun, ReconciliationStateError

_MAX_CONCURRENT_REVIEWS = 10


class RetryReviewRequest(BaseModel):
    """Bounded identity and event state for one explicit Check Run retry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    installation_id: int = Field(gt=0, strict=True)
    identity: ReviewIdentity
    check_run: CheckRun


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

    def get_check_run(self, *, check_run_id: int, installation_id: int) -> CheckRun: ...

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest: ...

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
        attempt_id: str | None = None,
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

    def __init__(  # noqa: PLR0913 - explicit durable lifecycle dependencies.
        self,
        *,
        github: CheckRunGateway,
        process: AttemptLauncher,
        reconciler: DesiredStateReconciler,
        active_attempts: ActiveAttemptRegistry | None = None,
        installation_id: int,
        max_concurrent_reviews: int = 1,
    ) -> None:
        if installation_id < 1:
            message = "installation_id must be positive"
            raise ValueError(message)
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
        self._active_attempts = active_attempts or VolatileActiveAttemptRegistry()
        self._installation_id = installation_id
        self._max_concurrent_reviews = max_concurrent_reviews
        self._lifecycle = _Lifecycle.CREATED
        self._admission_lock = asyncio.Lock()
        self._reserved = 0
        self._active_retry_check_runs: set[int] = set()
        self._attempt_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        if self._lifecycle is not _Lifecycle.CREATED:
            message = "review attempt coordinator cannot be restarted"
            raise RuntimeError(message)
        await self._reconciler.__aenter__()
        try:
            await self._recover_interrupted_attempts()
        except BaseException as error:
            await self._reconciler.__aexit__(
                type(error),
                error,
                error.__traceback__,
            )
            raise
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

    async def _recover_interrupted_attempts(self) -> None:
        for attempt in self._active_attempts.load():
            if attempt.check_run_id is None:
                check_runs = await asyncio.to_thread(
                    self._github.list_check_runs,
                    identity=attempt.identity,
                    installation_id=self._installation_id,
                )
                owned = tuple(
                    check_run
                    for check_run in check_runs
                    if self._github.is_owned_check_run(
                        check_run,
                        identity=attempt.identity,
                    )
                )
                if not owned:
                    self._active_attempts.finish(attempt_id=attempt.attempt_id)
                    continue
                if len(owned) != 1:
                    message = "active attempt matches multiple Check Runs"
                    raise RuntimeError(message)
                current = owned[0]
                self._active_attempts.bind(
                    attempt_id=attempt.attempt_id,
                    check_run_id=current.id,
                )
            else:
                current = await asyncio.to_thread(
                    self._github.get_check_run,
                    check_run_id=attempt.check_run_id,
                    installation_id=self._installation_id,
                )
            expected_check_run_id = attempt.check_run_id or current.id
            if current.id != expected_check_run_id or not self._github.is_owned_check_run(
                current,
                identity=attempt.identity,
            ):
                message = "active attempt does not match its Check Run"
                raise RuntimeError(message)
            if current.status is not CheckRunStatus.COMPLETED:
                await self._reconciler.set_desired(
                    DesiredCheckRun(
                        check_run_id=current.id,
                        identity=attempt.identity,
                        attempt_id=attempt.attempt_id,
                        output_kind=CheckRunOutputKind.TECHNICAL_FAILURE,
                        failure_stage="parent_restart",
                        failure_category="review_failure",
                    )
                )
            self._active_attempts.finish(attempt_id=attempt.attempt_id)

    async def start(  # noqa: PLR0911 - one admission transaction.
        self,
        request: ReviewRequest,
    ) -> SubmissionOutcome:
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
            attempt_id = uuid.uuid4().hex
            try:
                self._active_attempts.prepare(
                    ActiveAttempt(
                        identity=identity,
                        attempt_id=attempt_id,
                    )
                )
            except ActiveAttemptStateError:
                self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE
            try:
                check_run = await asyncio.to_thread(
                    self._github.create_check_run,
                    identity=identity,
                    installation_id=request.installation_id,
                )
            except GitHubError:
                with suppress(ActiveAttemptStateError):
                    self._active_attempts.finish(attempt_id=attempt_id)
                self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE

            try:
                self._active_attempts.bind(
                    attempt_id=attempt_id,
                    check_run_id=check_run.id,
                )
            except ActiveAttemptStateError:
                try:
                    await self._persist_terminal_and_finish(
                        DesiredCheckRun(
                            check_run_id=check_run.id,
                            identity=identity,
                            attempt_id=attempt_id,
                            output_kind=CheckRunOutputKind.TECHNICAL_FAILURE,
                            failure_stage="active_attempt_state",
                            failure_category="review_failure",
                        ),
                        attempt_id=attempt_id,
                    )
                finally:
                    self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE
            try:
                execution = await self._process.launch(
                    request,
                    check_run_id=check_run.id,
                    attempt_id=attempt_id,
                )
            except AttemptLaunchError as error:
                try:
                    await self._persist_terminal_and_finish(
                        _terminal_desired(
                            check_run_id=check_run.id,
                            identity=identity,
                            outcome=error.outcome,
                        ),
                        attempt_id=attempt_id,
                    )
                finally:
                    self._reserved -= 1
                return SubmissionOutcome.UNAVAILABLE

            with suppress(ReconciliationStateError):
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

    async def retry(  # noqa: PLR0911
        self,
        request: RetryReviewRequest,
    ) -> SubmissionOutcome:
        async with self._admission_lock:
            if self._lifecycle is not _Lifecycle.ACCEPTING:
                return SubmissionOutcome.STOPPING
            current_or_outcome = await self._current_retry_check_run(request)
            if isinstance(current_or_outcome, SubmissionOutcome):
                return current_or_outcome
            if self._reserved >= self._max_concurrent_reviews:
                return SubmissionOutcome.AT_CAPACITY

            current = current_or_outcome
            self._reserved += 1
            self._active_retry_check_runs.add(current.id)
            try:
                review_request = await asyncio.to_thread(
                    self._github.review_request,
                    pr_number=request.identity.pr_number,
                    installation_id=request.installation_id,
                )
            except GitHubError:
                self._reserved -= 1
                self._active_retry_check_runs.discard(current.id)
                return SubmissionOutcome.UNAVAILABLE
            if derive_review_identity(review_request) != request.identity:
                self._reserved -= 1
                self._active_retry_check_runs.discard(current.id)
                return SubmissionOutcome.ALREADY_REVIEWED

            attempt_id = uuid.uuid4().hex
            try:
                self._active_attempts.prepare(
                    ActiveAttempt(
                        identity=request.identity,
                        attempt_id=attempt_id,
                        check_run_id=current.id,
                    )
                )
            except ActiveAttemptStateError:
                self._reserved -= 1
                self._active_retry_check_runs.discard(current.id)
                return SubmissionOutcome.UNAVAILABLE
            try:
                await self._reconciler.set_desired(
                    DesiredCheckRun(
                        check_run_id=current.id,
                        identity=request.identity,
                        attempt_id=attempt_id,
                        output_kind=CheckRunOutputKind.QUEUED,
                    )
                )
            except ReconciliationStateError:
                with suppress(ActiveAttemptStateError):
                    self._active_attempts.finish(attempt_id=attempt_id)
                self._reserved -= 1
                self._active_retry_check_runs.discard(current.id)
                return SubmissionOutcome.UNAVAILABLE
            try:
                execution = await self._process.launch(
                    review_request,
                    check_run_id=current.id,
                    attempt_id=attempt_id,
                )
            except AttemptLaunchError as error:
                try:
                    await self._persist_terminal_and_finish(
                        _terminal_desired(
                            check_run_id=current.id,
                            identity=request.identity,
                            outcome=error.outcome,
                        ),
                        attempt_id=attempt_id,
                    )
                finally:
                    self._reserved -= 1
                    self._active_retry_check_runs.discard(current.id)
                return SubmissionOutcome.UNAVAILABLE

            with suppress(ReconciliationStateError):
                await self._reconciler.set_desired(
                    DesiredCheckRun(
                        check_run_id=current.id,
                        identity=request.identity,
                        attempt_id=execution.attempt_id,
                        output_kind=CheckRunOutputKind.RUNNING,
                    )
                )
            task = asyncio.create_task(
                self._complete_attempt(
                    execution,
                    check_run_id=current.id,
                    identity=request.identity,
                )
            )
            self._attempt_tasks.add(task)
            task.add_done_callback(self._attempt_tasks.discard)
            return SubmissionOutcome.ACCEPTED

    async def _current_retry_check_run(  # noqa: PLR0911
        self,
        request: RetryReviewRequest,
    ) -> CheckRun | SubmissionOutcome:
        if request.installation_id != self._installation_id:
            return SubmissionOutcome.ALREADY_REVIEWED
        if not self._github.is_owned_check_run(
            request.check_run,
            identity=request.identity,
        ):
            return SubmissionOutcome.ALREADY_REVIEWED
        if request.check_run.id in self._active_retry_check_runs:
            return SubmissionOutcome.ALREADY_RUNNING
        try:
            current = await asyncio.to_thread(
                self._github.get_check_run,
                check_run_id=request.check_run.id,
                installation_id=request.installation_id,
            )
        except GitHubError:
            return SubmissionOutcome.UNAVAILABLE
        if current.id != request.check_run.id or not self._github.is_owned_check_run(
            current,
            identity=request.identity,
        ):
            return SubmissionOutcome.ALREADY_REVIEWED
        if current.status is not CheckRunStatus.COMPLETED:
            return SubmissionOutcome.ALREADY_RUNNING
        if not _is_retryable(current):
            return SubmissionOutcome.ALREADY_REVIEWED
        return current

    async def _complete_attempt(
        self,
        execution: AttemptExecution,
        *,
        check_run_id: int,
        identity: ReviewIdentity,
    ) -> None:
        try:
            outcome = await execution.wait()
            await self._persist_terminal_and_finish(
                _terminal_desired(
                    check_run_id=check_run_id,
                    identity=identity,
                    outcome=outcome,
                ),
                attempt_id=execution.attempt_id,
            )
        finally:
            async with self._admission_lock:
                self._reserved -= 1
                self._active_retry_check_runs.discard(check_run_id)

    async def _persist_terminal_and_finish(
        self,
        desired: DesiredCheckRun,
        *,
        attempt_id: str,
    ) -> None:
        try:
            await self._reconciler.set_desired(desired)
        except ReconciliationStateError:
            return
        with suppress(ActiveAttemptStateError):
            self._active_attempts.finish(attempt_id=attempt_id)


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


def _is_retryable(check_run: CheckRun) -> bool:
    if (
        check_run.status is not CheckRunStatus.COMPLETED
        or check_run.conclusion != "neutral"
        or len(check_run.actions) != 1
    ):
        return False
    action = check_run.actions[0]
    return (
        action.label == "Retry review"
        and action.description == "Retry this incomplete advisory review."
        and action.identifier == "retry_review"
    )
