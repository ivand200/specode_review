import asyncio
import logging
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import ReviewRequest, ReviewResult
from review_agent.publishing import ReviewPublisher, publish_review_result

logger = logging.getLogger(__name__)
_WAITING_CAPACITY = 10
_STOP = object()


class SubmissionOutcome(Enum):
    ACCEPTED = auto()
    AT_CAPACITY = auto()
    STOPPING = auto()


class ReviewWorker(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def submit(self, request: ReviewRequest) -> SubmissionOutcome: ...


class ReviewService(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


class _Lifecycle(Enum):
    CREATED = auto()
    ACCEPTING = auto()
    STOPPING = auto()
    STOPPED = auto()


class SingleReviewWorker:
    def __init__(
        self,
        *,
        reviewer: ReviewService,
        publisher: ReviewPublisher,
        review_timeout_seconds: float,
    ) -> None:
        if review_timeout_seconds <= 0:
            message = "review timeout must be positive"
            raise ValueError(message)
        self._reviewer = reviewer
        self._publisher = publisher
        self._review_timeout_seconds = review_timeout_seconds
        self._queue: asyncio.Queue[ReviewRequest | object] = asyncio.Queue(
            maxsize=_WAITING_CAPACITY
        )
        self._lifecycle = _Lifecycle.CREATED
        self._processing_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._lifecycle is not _Lifecycle.CREATED:
            message = "single review worker cannot be restarted"
            raise RuntimeError(message)
        self._lifecycle = _Lifecycle.ACCEPTING
        self._processing_task = asyncio.create_task(self._process())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._lifecycle = _Lifecycle.STOPPING
        self._discard_waiting_requests()
        self._queue.put_nowait(_STOP)
        processing_task = self._processing_task
        if processing_task is None:
            message = "single review worker was not started"
            raise RuntimeError(message)
        try:
            try:
                await asyncio.wait_for(
                    asyncio.shield(processing_task),
                    timeout=self._review_timeout_seconds,
                )
            except TimeoutError:
                processing_task.cancel()
            with suppress(asyncio.CancelledError):
                await processing_task
        finally:
            self._lifecycle = _Lifecycle.STOPPED

    def submit(self, request: ReviewRequest) -> SubmissionOutcome:
        if self._lifecycle is not _Lifecycle.ACCEPTING:
            return SubmissionOutcome.STOPPING
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            return SubmissionOutcome.AT_CAPACITY
        return SubmissionOutcome.ACCEPTED

    async def _process(self) -> None:
        while True:
            request = await self._queue.get()
            try:
                if request is _STOP:
                    return
                if not isinstance(request, ReviewRequest):
                    message = "single review worker received an invalid queue item"
                    raise TypeError(message)
                deadline = ReviewDeadline.after(self._review_timeout_seconds)
                await asyncio.to_thread(self._run_attempt, request, deadline)
            finally:
                self._queue.task_done()

    def _run_attempt(
        self,
        request: ReviewRequest,
        deadline: ReviewDeadline,
    ) -> None:
        stage = "review"
        with review_deadline_scope(deadline):
            try:
                deadline.remaining(stage=stage)
                result = self._reviewer.review(request)
                deadline.remaining(stage=stage)
                stage = "publication"
                deadline.remaining(stage=stage)
                publish_review_result(
                    result,
                    self._publisher,
                    installation_id=request.installation_id,
                )
            except asyncio.CancelledError:
                self._log_failure(
                    request,
                    stage=stage,
                    category=FailureCategory.REVIEW_FAILURE,
                )
            except ReviewError as error:
                self._log_failure(
                    request,
                    stage=error.stage,
                    category=error.category,
                )
            except TimeoutError:
                self._log_failure(
                    request,
                    stage=stage,
                    category=FailureCategory.TIMEOUT,
                )
            except Exception:  # noqa: BLE001 - worker failure-isolation boundary.
                self._log_failure(
                    request,
                    stage=stage,
                    category=FailureCategory.REVIEW_FAILURE,
                )

    def _discard_waiting_requests(self) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if not isinstance(request, ReviewRequest):
                    continue
                self._log_failure(
                    request,
                    stage="worker_shutdown",
                    category=FailureCategory.REVIEW_FAILURE,
                )
            finally:
                self._queue.task_done()

    @staticmethod
    def _log_failure(
        request: ReviewRequest,
        *,
        stage: str,
        category: FailureCategory,
    ) -> None:
        logger.warning(
            "review failed repository=%s pr_number=%d head_sha=%s stage=%s category=%s",
            request.repository,
            request.pr_number,
            request.head_sha,
            stage,
            category.value,
        )
