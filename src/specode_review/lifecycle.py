import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from specode_review.accepted_revision import AcceptedRevision
from specode_review.lifecycle_evidence import (
    emit_lifecycle_evidence,
    emit_normalized_failure,
)
from specode_review.models import ReviewRequest
from specode_review.review_runner import PreflightOutcome, ReviewCompletion
from specode_review.submission import SubmissionOutcome

DEFAULT_MAX_CONCURRENT_REVIEWS = 3
MIN_CONCURRENT_REVIEWS = 1
MAX_CONCURRENT_REVIEWS = 5


class _Runner(Protocol):
    def preflight(self, request: ReviewRequest) -> PreflightOutcome: ...

    def run(self, request: ReviewRequest, attempt_id: str) -> object: ...


class _LifecycleState(Enum):
    CREATED = auto()
    ACCEPTING = auto()
    STOPPING = auto()
    STOPPED = auto()


class _ClaimPhase(Enum):
    PREFLIGHT = auto()
    RUNNING = auto()


class ReviewLifecycle:
    """Own in-process review admission, task lifetime, and graceful shutdown."""

    def __init__(
        self,
        *,
        runner: _Runner,
        max_concurrent_reviews: int = DEFAULT_MAX_CONCURRENT_REVIEWS,
        attempt_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if (
            isinstance(max_concurrent_reviews, bool)
            or not MIN_CONCURRENT_REVIEWS
            <= max_concurrent_reviews
            <= MAX_CONCURRENT_REVIEWS
        ):
            message = (
                "max_concurrent_reviews must be between "
                f"{MIN_CONCURRENT_REVIEWS} and {MAX_CONCURRENT_REVIEWS}"
            )
            raise ValueError(message)
        self._runner = runner
        self._max_concurrent_reviews = max_concurrent_reviews
        self._attempt_id_factory = attempt_id_factory or _new_attempt_id
        self._state = _LifecycleState.CREATED
        self._claims: dict[AcceptedRevision, _ClaimPhase] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._condition = asyncio.Condition()

    async def __aenter__(self) -> Self:
        async with self._condition:
            if self._state is not _LifecycleState.CREATED:
                message = "review lifecycle cannot be restarted"
                raise RuntimeError(message)
            self._state = _LifecycleState.ACCEPTING
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        async with self._condition:
            self._state = _LifecycleState.STOPPING
            await self._condition.wait_for(lambda: not self._claims and not self._tasks)
            self._state = _LifecycleState.STOPPED

    async def submit(self, request: ReviewRequest) -> SubmissionOutcome:
        identity = AcceptedRevision.from_review_request(request)
        immediate_outcome: SubmissionOutcome | None = None
        async with self._condition:
            if self._state is not _LifecycleState.ACCEPTING:
                immediate_outcome = SubmissionOutcome.STOPPING
            elif identity in self._claims:
                immediate_outcome = SubmissionOutcome.ALREADY_RUNNING
            else:
                self._claims[identity] = _ClaimPhase.PREFLIGHT
        if immediate_outcome is not None:
            _emit_admission(request, immediate_outcome)
            return immediate_outcome

        try:
            return await self._submit_claimed(request, identity)
        except asyncio.CancelledError:
            await self._release_claim(identity)
            raise

    async def _submit_claimed(  # noqa: PLR0911 - specified admission dispositions.
        self,
        request: ReviewRequest,
        identity: AcceptedRevision,
    ) -> SubmissionOutcome:
        preflight_started = time.monotonic()
        preflight = await self._preflight(request, identity)
        emit_lifecycle_evidence(
            request,
            "preflight",
            terminal_outcome=(
                preflight.name.lower()
                if isinstance(preflight, SubmissionOutcome)
                else preflight.value
            ),
            duration_ms=_duration_ms(preflight_started),
        )
        if isinstance(preflight, SubmissionOutcome):
            _emit_admission(request, preflight)
            return preflight

        if preflight is PreflightOutcome.ALREADY_REVIEWED:
            await self._release_claim(identity)
            outcome = SubmissionOutcome.ALREADY_REVIEWED
            _emit_admission(request, outcome)
            return outcome
        if preflight is PreflightOutcome.NOT_AUTHORIZED:
            await self._release_claim(identity)
            outcome = SubmissionOutcome.NOT_AUTHORIZED
            _emit_admission(request, outcome)
            return outcome
        if preflight is not PreflightOutcome.READY:
            await self._release_claim(identity)
            outcome = SubmissionOutcome.UNAVAILABLE
            _emit_admission(request, outcome)
            return outcome

        try:
            attempt_id = self._attempt_id_factory()
        except Exception as error:  # noqa: BLE001 - keep unexpected admission failures bounded.
            await self._release_claim(identity)
            emit_normalized_failure(
                request,
                error,
                fallback_stage="attempt_construction",
            )
            outcome = SubmissionOutcome.UNAVAILABLE
            _emit_admission(request, outcome)
            return outcome

        promotion_outcome: SubmissionOutcome | None = None
        async with self._condition:
            if self._state is not _LifecycleState.ACCEPTING:
                self._claims.pop(identity, None)
                self._condition.notify_all()
                promotion_outcome = SubmissionOutcome.STOPPING
            elif (
                sum(phase is _ClaimPhase.RUNNING for phase in self._claims.values())
                >= self._max_concurrent_reviews
            ):
                self._claims.pop(identity, None)
                self._condition.notify_all()
                promotion_outcome = SubmissionOutcome.AT_CAPACITY
            else:
                self._claims[identity] = _ClaimPhase.RUNNING
                task = asyncio.create_task(self._run(identity, request, attempt_id))
                self._tasks.add(task)
        if promotion_outcome is not None:
            _emit_admission(request, promotion_outcome)
            return promotion_outcome
        emit_lifecycle_evidence(
            request,
            "running",
            attempt_id=attempt_id,
            terminal_outcome="started",
        )
        _emit_admission(request, SubmissionOutcome.ACCEPTED, attempt_id=attempt_id)
        return SubmissionOutcome.ACCEPTED

    async def _preflight(
        self,
        request: ReviewRequest,
        identity: AcceptedRevision,
    ) -> PreflightOutcome | SubmissionOutcome:
        preflight_task = asyncio.create_task(
            asyncio.to_thread(self._runner.preflight, request)
        )
        try:
            return await asyncio.shield(preflight_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await preflight_task
            raise
        except Exception as error:  # noqa: BLE001 - normalize the configured runner seam.
            await self._release_claim(identity)
            emit_normalized_failure(
                request,
                error,
                fallback_stage="preflight",
            )
            return SubmissionOutcome.UNAVAILABLE

    async def _run(
        self,
        identity: AcceptedRevision,
        request: ReviewRequest,
        attempt_id: str,
    ) -> None:
        started = time.monotonic()
        terminal_facts: dict[str, str | int] = {"terminal_outcome": "succeeded"}
        try:
            completion = await asyncio.to_thread(self._runner.run, request, attempt_id)
            if isinstance(completion, ReviewCompletion):
                terminal_facts["publication_disposition"] = completion.publication.value
        except Exception as error:  # noqa: BLE001 - consume normalized failures.
            terminal_facts["terminal_outcome"] = "failed"
            emit_normalized_failure(
                request,
                error,
                fallback_stage="review",
                attempt_id=attempt_id,
            )
        finally:
            current = asyncio.current_task()
            async with self._condition:
                self._claims.pop(identity, None)
                if current is not None:
                    self._tasks.discard(current)
                self._condition.notify_all()
            emit_lifecycle_evidence(
                request,
                "terminal_release",
                attempt_id=attempt_id,
                duration_ms=_duration_ms(started),
                **terminal_facts,
            )

    async def _release_claim(self, identity: AcceptedRevision) -> None:
        async with self._condition:
            self._claims.pop(identity, None)
            self._condition.notify_all()


def _new_attempt_id() -> str:
    return uuid.uuid4().hex


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


def _emit_admission(
    request: ReviewRequest,
    outcome: SubmissionOutcome,
    *,
    attempt_id: str | None = None,
) -> None:
    emit_lifecycle_evidence(
        request,
        "admission",
        attempt_id=attempt_id,
        admission_disposition=outcome.name.lower(),
    )
