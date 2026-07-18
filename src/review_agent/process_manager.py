import asyncio
import logging
import os
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from review_agent.attempt import AttemptCommand
from review_agent.configuration import AttemptSettings
from review_agent.models import ReviewRequest
from review_agent.resources import ReviewResourceManager

logger = logging.getLogger(__name__)


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
        self._lifecycle = _Lifecycle.CREATED
        self._reserved = False
        self._monitor_task: asyncio.Task[None] | None = None

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
        self._lifecycle = _Lifecycle.STOPPING
        monitor_task = self._monitor_task
        try:
            if monitor_task is not None:
                await monitor_task
        finally:
            self._lifecycle = _Lifecycle.STOPPED

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:
        if self._lifecycle is not _Lifecycle.ACCEPTING:
            return SubmissionOutcome.STOPPING
        if self._reserved:
            return SubmissionOutcome.AT_CAPACITY

        self._reserved = True
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
            process.stdin.write(command.to_json_bytes())
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except OSError:
            await self._rollback_failed_launch(process, attempt_id=attempt_id)
            logger.warning(
                "review process unavailable attempt_id=%s stage=launch category=review_failure",
                attempt_id,
            )
            return SubmissionOutcome.UNAVAILABLE

        self._monitor_task = asyncio.create_task(
            self._monitor(process, command=command)
        )
        return SubmissionOutcome.ACCEPTED

    async def _rollback_failed_launch(
        self,
        process: asyncio.subprocess.Process | None,
        *,
        attempt_id: str,
    ) -> None:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
        await self._cleanup(attempt_id)
        self._reserved = False

    async def _monitor(
        self,
        process: asyncio.subprocess.Process,
        *,
        command: AttemptCommand,
    ) -> None:
        return_code = await process.wait()
        request = command.request
        if return_code == 0:
            outcome = "success"
        elif return_code < 0:
            outcome = f"signal_{-return_code}"
        else:
            outcome = "nonzero_exit"
        logger.info(
            "review process exited attempt_id=%s repository=%s pr_number=%d "
            "base_sha=%s head_sha=%s outcome=%s",
            command.attempt_id,
            request.repository,
            request.pr_number,
            request.base_sha,
            request.head_sha,
            outcome,
        )
        try:
            await self._cleanup(command.attempt_id)
        finally:
            self._reserved = False
            self._monitor_task = None

    async def _cleanup(self, attempt_id: str) -> None:
        try:
            await asyncio.to_thread(self._resource_manager.cleanup, attempt_id)
        except Exception:  # noqa: BLE001 - exact parent cleanup failure boundary.
            logger.warning(
                "review process cleanup failed "
                "attempt_id=%s stage=cleanup category=review_failure",
                attempt_id,
            )
