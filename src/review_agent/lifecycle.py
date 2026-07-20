import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from review_agent.github import ReviewIdentity, derive_review_identity
from review_agent.models import ReviewRequest
from review_agent.review_runner import PreflightOutcome
from review_agent.submission import SubmissionOutcome

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
        self._claims: dict[ReviewIdentity, _ClaimPhase] = {}
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
        identity = derive_review_identity(request)
        async with self._condition:
            if self._state is not _LifecycleState.ACCEPTING:
                return SubmissionOutcome.STOPPING
            if identity in self._claims:
                return SubmissionOutcome.ALREADY_RUNNING
            self._claims[identity] = _ClaimPhase.PREFLIGHT

        try:
            return await self._submit_claimed(request, identity)
        except asyncio.CancelledError:
            await self._release_claim(identity)
            raise

    async def _submit_claimed(  # noqa: PLR0911 - specified admission dispositions.
        self,
        request: ReviewRequest,
        identity: ReviewIdentity,
    ) -> SubmissionOutcome:
        preflight = await self._preflight(request, identity)
        if isinstance(preflight, SubmissionOutcome):
            return preflight

        if preflight is PreflightOutcome.ALREADY_REVIEWED:
            await self._release_claim(identity)
            return SubmissionOutcome.ALREADY_REVIEWED
        if preflight is PreflightOutcome.NOT_AUTHORIZED:
            await self._release_claim(identity)
            return SubmissionOutcome.NOT_AUTHORIZED
        if preflight is not PreflightOutcome.READY:
            await self._release_claim(identity)
            return SubmissionOutcome.UNAVAILABLE

        try:
            attempt_id = self._attempt_id_factory()
        except Exception:  # noqa: BLE001 - keep unexpected admission failures bounded.
            await self._release_claim(identity)
            return SubmissionOutcome.UNAVAILABLE

        async with self._condition:
            if self._state is not _LifecycleState.ACCEPTING:
                self._claims.pop(identity, None)
                self._condition.notify_all()
                return SubmissionOutcome.STOPPING
            running = sum(phase is _ClaimPhase.RUNNING for phase in self._claims.values())
            if running >= self._max_concurrent_reviews:
                self._claims.pop(identity, None)
                self._condition.notify_all()
                return SubmissionOutcome.AT_CAPACITY

            self._claims[identity] = _ClaimPhase.RUNNING
            task = asyncio.create_task(self._run(identity, request, attempt_id))
            self._tasks.add(task)
            return SubmissionOutcome.ACCEPTED

    async def _preflight(
        self,
        request: ReviewRequest,
        identity: ReviewIdentity,
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
        except Exception:  # noqa: BLE001 - normalize the configured runner seam.
            await self._release_claim(identity)
            return SubmissionOutcome.UNAVAILABLE

    async def _run(
        self,
        identity: ReviewIdentity,
        request: ReviewRequest,
        attempt_id: str,
    ) -> None:
        try:
            await asyncio.to_thread(self._runner.run, request, attempt_id)
        except Exception:  # noqa: BLE001 - the retained task must consume normalized failures.
            return
        finally:
            current = asyncio.current_task()
            async with self._condition:
                self._claims.pop(identity, None)
                if current is not None:
                    self._tasks.discard(current)
                self._condition.notify_all()

    async def _release_claim(self, identity: ReviewIdentity) -> None:
        async with self._condition:
            self._claims.pop(identity, None)
            self._condition.notify_all()


def _new_attempt_id() -> str:
    return uuid.uuid4().hex
