import asyncio
import logging
import os
import signal
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol

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


@dataclass(frozen=True, slots=True)
class _RunningOutcomeAttempt:
    process: asyncio.subprocess.Process
    reader: asyncio.Task[bytes]
    hard_deadline: float


class AttemptExecution(Protocol):
    attempt_id: str

    async def wait(self) -> AttemptOutcome: ...


class AttemptLaunchError(RuntimeError):
    """A normalized failed launch that still identifies the attempted execution."""

    def __init__(self, outcome: AttemptOutcome) -> None:
        self.outcome = outcome
        super().__init__("review attempt launch failed")


@dataclass(slots=True)
class _ProcessAttemptExecution:
    attempt_id: str
    running: _RunningOutcomeAttempt
    manager: "ReviewProcessManager"
    _waited: bool = False

    async def wait(self) -> AttemptOutcome:
        if self._waited:
            message = "review attempt result can only be consumed once"
            raise RuntimeError(message)
        self._waited = True
        outcome = await self.manager._consume_outcome_attempt(  # noqa: SLF001
            self.running,
            attempt_id=self.attempt_id,
        )
        cleanup_succeeded = await self.manager._cleanup(self.attempt_id)  # noqa: SLF001
        if not cleanup_succeeded and outcome.status is AttemptStatus.REVIEWED:
            return _normalized_incomplete_outcome(
                self.attempt_id,
                stage="cleanup",
                category=FailureCategory.REVIEW_FAILURE,
                publication=AttemptPublication.PUBLISHED,
                review_status=outcome.review_status,
            )
        return outcome


class ReviewProcessManager:
    """Own the operating-system lifetime of complete review attempts."""

    def __init__(
        self,
        *,
        attempt_settings: AttemptSettings,
        resource_manager: ReviewResourceManager,
        parent_environment: Mapping[str, str] | None = None,
        child_arguments: tuple[str, ...] | None = None,
    ) -> None:
        resolved_arguments = child_arguments or (
            sys.executable,
            "-m",
            "review_agent.attempt",
        )
        if not resolved_arguments:
            message = "review child arguments cannot be empty"
            raise ValueError(message)
        self._attempt_settings = attempt_settings
        self._resource_manager = resource_manager
        self._executor_environment = attempt_settings.render_executor_environment(
            os.environ if parent_environment is None else parent_environment
        )
        self._child_arguments = resolved_arguments

    async def launch(
        self,
        request: ReviewRequest,
        *,
        check_run_id: int,
        attempt_id: str | None = None,
    ) -> AttemptExecution:
        """Launch one Check Run attempt, returning after its command is accepted."""
        resolved_attempt_id = attempt_id or uuid.uuid4().hex
        hard_deadline = (
            asyncio.get_running_loop().time()
            + self._attempt_settings.review_timeout_seconds
            + self._attempt_settings.sandbox_cleanup_timeout_seconds
        )
        running = await self._launch_outcome_attempt(
            request,
            attempt_id=resolved_attempt_id,
            check_run_id=check_run_id,
            hard_deadline=hard_deadline,
        )
        if running is None:
            raise AttemptLaunchError(
                _normalized_incomplete_outcome(
                    resolved_attempt_id,
                    stage="launch",
                    category=FailureCategory.REVIEW_FAILURE,
                    publication=AttemptPublication.NOT_ATTEMPTED,
                )
            )
        return _ProcessAttemptExecution(
            attempt_id=resolved_attempt_id,
            running=running,
            manager=self,
        )

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
            grace_seconds=self._attempt_settings.sandbox_cleanup_timeout_seconds,
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
                grace_seconds=self._attempt_settings.sandbox_cleanup_timeout_seconds,
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

    async def _cleanup(self, attempt_id: str) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._resource_manager.cleanup, attempt_id),
                timeout=self._attempt_settings.sandbox_cleanup_timeout_seconds,
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
