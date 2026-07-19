import asyncio
import logging
import os
import signal
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from types import TracebackType
from typing import Literal, Protocol, Self

from review_agent.attempt import (
    ATTEMPT_OUTCOME_MAX_BYTES,
    AttemptCommand,
    AttemptOutcome,
    AttemptOutcomeError,
    AttemptPublication,
    AttemptStatus,
)
from review_agent.configuration import AttemptSettings
from review_agent.errors import FailureCategory
from review_agent.models import ReviewRequest
from review_agent.resources import ReviewResourceManager

logger = logging.getLogger(__name__)

type _ActiveReviewKey = tuple[str, int, str, str]
_MAX_CONCURRENT_REVIEWS = 10


class SubmissionOutcome(Enum):
    ACCEPTED = auto()
    ALREADY_RUNNING = auto()
    AT_CAPACITY = auto()
    STOPPING = auto()
    UNAVAILABLE = auto()


class ReviewExecutionManager(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def start(self, request: ReviewRequest) -> SubmissionOutcome: ...


class _Lifecycle(Enum):
    CREATED = auto()
    ACCEPTING = auto()
    STOPPING = auto()
    STOPPED = auto()


@dataclass(frozen=True, slots=True)
class _RunningOutcomeAttempt:
    process: asyncio.subprocess.Process
    reader: asyncio.Task[bytes]
    hard_deadline: float


class ReviewProcessManager:
    """Own the operating-system lifetime of complete review attempts."""

    def __init__(
        self,
        *,
        attempt_settings: AttemptSettings,
        resource_manager: ReviewResourceManager,
        parent_environment: Mapping[str, str] | None = None,
        child_arguments: tuple[str, ...] | None = None,
        max_concurrent_reviews: int = 1,
    ) -> None:
        resolved_arguments = child_arguments or (
            sys.executable,
            "-m",
            "review_agent.attempt",
        )
        if not resolved_arguments:
            message = "review child arguments cannot be empty"
            raise ValueError(message)
        if (
            isinstance(max_concurrent_reviews, bool)
            or not isinstance(max_concurrent_reviews, int)
            or not 1 <= max_concurrent_reviews <= _MAX_CONCURRENT_REVIEWS
        ):
            message = "maximum concurrent reviews must be between one and ten"
            raise ValueError(message)
        self._attempt_settings = attempt_settings
        self._resource_manager = resource_manager
        self._executor_environment = attempt_settings.render_executor_environment(
            os.environ if parent_environment is None else parent_environment
        )
        self._child_arguments = resolved_arguments
        self._max_concurrent_reviews = max_concurrent_reviews
        self._lifecycle = _Lifecycle.CREATED
        self._admission_lock = asyncio.Lock()
        self._reserved = 0
        self._active_keys: set[_ActiveReviewKey] = set()
        self._monitor_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> Self:
        if self._lifecycle is not _Lifecycle.CREATED:
            message = "review process manager cannot be restarted"
            raise RuntimeError(message)
        self._lifecycle = _Lifecycle.ACCEPTING
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        async with self._admission_lock:
            self._lifecycle = _Lifecycle.STOPPING
            monitor_tasks = tuple(self._monitor_tasks)
        try:
            if monitor_tasks:
                await asyncio.gather(*monitor_tasks)
        finally:
            self._lifecycle = _Lifecycle.STOPPED

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:
        async with self._admission_lock:
            if self._lifecycle is not _Lifecycle.ACCEPTING:
                return SubmissionOutcome.STOPPING
            active_key = (
                request.repository,
                request.pr_number,
                request.base_sha,
                request.head_sha,
            )
            if active_key in self._active_keys:
                return SubmissionOutcome.ALREADY_RUNNING
            if self._reserved >= self._max_concurrent_reviews:
                return SubmissionOutcome.AT_CAPACITY

            self._reserved += 1
            self._active_keys.add(active_key)
            attempt_id = uuid.uuid4().hex
            command = AttemptCommand(attempt_id=attempt_id, request=request)
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._child_arguments,
                    stdin=asyncio.subprocess.PIPE,
                    env=self._executor_environment,
                    start_new_session=True,
                )
                hard_deadline = (
                    asyncio.get_running_loop().time()
                    + self._attempt_settings.runtime.review_timeout_seconds
                    + self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
                )
                logger.info(
                    "review process started attempt_id=%s repository=%s pr_number=%d "
                    "base_sha=%s head_sha=%s pid=%d",
                    attempt_id,
                    request.repository,
                    request.pr_number,
                    request.base_sha,
                    request.head_sha,
                    process.pid,
                )
                if process.stdin is None:
                    message = "review child stdin was not created"
                    raise BrokenPipeError(message)
                async with asyncio.timeout_at(hard_deadline):
                    process.stdin.write(command.to_json_bytes())
                    await process.stdin.drain()
                    process.stdin.close()
                    await process.stdin.wait_closed()
            except asyncio.CancelledError:
                await self._rollback_failed_launch(
                    process,
                    attempt_id=attempt_id,
                    active_key=active_key,
                )
                raise
            except (OSError, TimeoutError):
                await self._rollback_failed_launch(
                    process,
                    attempt_id=attempt_id,
                    active_key=active_key,
                )
                logger.warning(
                    "review process unavailable attempt_id=%s stage=launch category=review_failure",
                    attempt_id,
                )
                return SubmissionOutcome.UNAVAILABLE

            monitor_task = asyncio.create_task(
                self._monitor(
                    process,
                    command=command,
                    hard_deadline=hard_deadline,
                )
            )
            self._monitor_tasks.add(monitor_task)
            return SubmissionOutcome.ACCEPTED

    async def execute(
        self,
        request: ReviewRequest,
        *,
        check_run_id: int,
    ) -> AttemptOutcome:
        """Execute one Check Run attempt and return only its normalized result."""
        attempt_id = uuid.uuid4().hex
        hard_deadline = (
            asyncio.get_running_loop().time()
            + self._attempt_settings.runtime.review_timeout_seconds
            + self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
        )
        running = await self._launch_outcome_attempt(
            request,
            attempt_id=attempt_id,
            check_run_id=check_run_id,
            hard_deadline=hard_deadline,
        )
        if running is None:
            return _normalized_incomplete_outcome(
                attempt_id,
                stage="launch",
                category=FailureCategory.REVIEW_FAILURE,
                publication=AttemptPublication.NOT_ATTEMPTED,
            )
        outcome = await self._consume_outcome_attempt(running, attempt_id=attempt_id)
        cleanup_succeeded = await self._cleanup(attempt_id)
        if not cleanup_succeeded and outcome.status is AttemptStatus.REVIEWED:
            return _normalized_incomplete_outcome(
                attempt_id,
                stage="cleanup",
                category=FailureCategory.REVIEW_FAILURE,
                publication=AttemptPublication.PUBLISHED,
                review_status=outcome.review_status,
            )
        return outcome

    async def _launch_outcome_attempt(
        self,
        request: ReviewRequest,
        *,
        attempt_id: str,
        check_run_id: int,
        hard_deadline: float,
    ) -> _RunningOutcomeAttempt | None:
        process: asyncio.subprocess.Process | None = None
        read_fd = -1
        write_fd = -1
        reader: asyncio.Task[bytes] | None = None
        try:
            read_fd, write_fd = os.pipe()
            command = AttemptCommand(
                attempt_id=attempt_id,
                check_run_id=check_run_id,
                outcome_fd=write_fd,
                request=request,
            )
            process = await asyncio.create_subprocess_exec(
                *self._child_arguments,
                stdin=asyncio.subprocess.PIPE,
                env=self._executor_environment,
                start_new_session=True,
                pass_fds=(write_fd,),
            )
            os.close(write_fd)
            write_fd = -1
            reader = asyncio.create_task(asyncio.to_thread(_read_bounded_outcome, read_fd))
            read_fd = -1
            if process.stdin is None:
                message = "review child stdin was not created"
                raise BrokenPipeError(message)
            async with asyncio.timeout_at(hard_deadline):
                process.stdin.write(command.to_json_bytes())
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
        except asyncio.CancelledError:
            await self._stop_outcome_process(process, attempt_id=attempt_id)
            if reader is not None:
                await reader
            raise
        except (OSError, TimeoutError):
            await self._stop_outcome_process(process, attempt_id=attempt_id)
            if reader is not None:
                await reader
            await self._cleanup(attempt_id)
            return None
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if read_fd >= 0:
                os.close(read_fd)
        if reader is None or process is None:
            message = "outcome process launch invariant violated"
            raise RuntimeError(message)
        return _RunningOutcomeAttempt(
            process=process,
            reader=reader,
            hard_deadline=hard_deadline,
        )

    async def _stop_outcome_process(
        self,
        process: asyncio.subprocess.Process | None,
        *,
        attempt_id: str,
    ) -> None:
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is not None:
            await process.wait()
            return
        await _terminate_process_group(
            process,
            attempt_id=attempt_id,
            grace_seconds=(
                self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
            ),
        )

    async def _consume_outcome_attempt(
        self,
        running: _RunningOutcomeAttempt,
        *,
        attempt_id: str,
    ) -> AttemptOutcome:
        remaining = max(
            0.0,
            running.hard_deadline - asyncio.get_running_loop().time(),
        )
        hard_timed_out = False
        try:
            await asyncio.wait_for(running.process.wait(), timeout=remaining)
        except TimeoutError:
            hard_timed_out = True
            await _terminate_process_group(
                running.process,
                attempt_id=attempt_id,
                grace_seconds=(
                    self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
                ),
            )

        document = await running.reader
        if hard_timed_out:
            return _normalized_incomplete_outcome(
                attempt_id,
                stage="timeout",
                category=FailureCategory.TIMEOUT,
                publication=AttemptPublication.UNKNOWN,
            )
        try:
            return AttemptOutcome.from_json_bytes(
                document,
                expected_attempt_id=attempt_id,
            )
        except AttemptOutcomeError:
            return _normalized_incomplete_outcome(
                attempt_id,
                stage="child_outcome",
                category=FailureCategory.REVIEW_FAILURE,
                publication=AttemptPublication.UNKNOWN,
            )

    async def _rollback_failed_launch(
        self,
        process: asyncio.subprocess.Process | None,
        *,
        attempt_id: str,
        active_key: _ActiveReviewKey,
    ) -> None:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                await _terminate_process_group(
                    process,
                    attempt_id=attempt_id,
                    grace_seconds=(
                        self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
                    ),
                )
            else:
                await process.wait()
        await self._cleanup(attempt_id)
        self._reserved -= 1
        self._active_keys.remove(active_key)

    async def _monitor(
        self,
        process: asyncio.subprocess.Process,
        *,
        command: AttemptCommand,
        hard_deadline: float,
    ) -> None:
        remaining = max(0.0, hard_deadline - asyncio.get_running_loop().time())
        hard_timed_out = False
        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=remaining)
        except TimeoutError:
            hard_timed_out = True
            logger.warning(
                "review process hard timeout attempt_id=%s stage=timeout category=review_failure",
                command.attempt_id,
            )
            cleanup_grace = self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds
            return_code = await _terminate_process_group(
                process,
                attempt_id=command.attempt_id,
                grace_seconds=cleanup_grace,
            )
        request = command.request
        if return_code == 0:
            child_status = "success"
        elif return_code < 0:
            child_status = f"signal_{-return_code}"
        else:
            child_status = "nonzero_exit"
        if hard_timed_out:
            logger.info(
                "review process exited attempt_id=%s repository=%s pr_number=%d "
                "base_sha=%s head_sha=%s outcome=hard_timeout child_status=%s",
                command.attempt_id,
                request.repository,
                request.pr_number,
                request.base_sha,
                request.head_sha,
                child_status,
            )
        else:
            logger.info(
                "review process exited attempt_id=%s repository=%s pr_number=%d "
                "base_sha=%s head_sha=%s outcome=%s",
                command.attempt_id,
                request.repository,
                request.pr_number,
                request.base_sha,
                request.head_sha,
                child_status,
            )
        try:
            cleanup_succeeded = await self._cleanup(command.attempt_id)
            if hard_timed_out and cleanup_succeeded:
                logger.info(
                    "review process cleanup completed attempt_id=%s stage=cleanup outcome=success",
                    command.attempt_id,
                )
        finally:
            async with self._admission_lock:
                self._reserved -= 1
                active_key = (
                    request.repository,
                    request.pr_number,
                    request.base_sha,
                    request.head_sha,
                )
                self._active_keys.remove(active_key)
                self._monitor_tasks.discard(asyncio.current_task())

    async def _cleanup(self, attempt_id: str) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._resource_manager.cleanup, attempt_id),
                timeout=(self._attempt_settings.runtime.sandbox_operation.cleanup_timeout_seconds),
            )
        except Exception:  # noqa: BLE001 - exact parent cleanup failure boundary.
            logger.warning(
                "review process cleanup failed attempt_id=%s stage=cleanup category=review_failure",
                attempt_id,
            )
            return False
        return True


def _read_bounded_outcome(read_fd: int) -> bytes:
    document = bytearray()
    try:
        while len(document) <= ATTEMPT_OUTCOME_MAX_BYTES:
            chunk = os.read(
                read_fd,
                min(1_024, ATTEMPT_OUTCOME_MAX_BYTES + 1 - len(document)),
            )
            if not chunk:
                break
            document.extend(chunk)
    finally:
        os.close(read_fd)
    return bytes(document)


def _normalized_incomplete_outcome(
    attempt_id: str,
    *,
    stage: str,
    category: FailureCategory,
    publication: AttemptPublication,
    review_status: Literal["no_important_issues", "issues_found"] | None = None,
) -> AttemptOutcome:
    return AttemptOutcome(
        attempt_id=attempt_id,
        status=(
            AttemptStatus.TIMED_OUT
            if category is FailureCategory.TIMEOUT
            else AttemptStatus.FAILED
        ),
        review_status=review_status,
        publication=publication,
        failure_stage=stage,
        failure_category=category,
    )


async def _wait_for_process_group_exit(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float,
) -> int | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_seconds
    while _process_group_exists(process.pid):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.01, remaining))
    return await process.wait()


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    attempt_id: str,
    grace_seconds: float,
) -> int:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
        logger.info(
            "review process signal sent attempt_id=%s signal=SIGTERM",
            attempt_id,
        )
    return_code = await _wait_for_process_group_exit(
        process,
        grace_seconds=grace_seconds,
    )
    if return_code is not None:
        return return_code
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
        logger.info(
            "review process signal sent attempt_id=%s signal=SIGKILL",
            attempt_id,
        )
    return await process.wait()


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
