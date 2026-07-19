from enum import Enum, auto
from types import TracebackType
from typing import Protocol, Self

from review_agent.models import ReviewRequest


class SubmissionOutcome(Enum):
    """Webhook-facing result of a coordinator admission decision."""

    ACCEPTED = auto()
    ALREADY_RUNNING = auto()
    ALREADY_REVIEWED = auto()
    AT_CAPACITY = auto()
    STOPPING = auto()
    UNAVAILABLE = auto()


class ReviewSubmissionManager(Protocol):
    """Application lifecycle and initial-review admission used by the webhook."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def start(self, request: ReviewRequest) -> SubmissionOutcome: ...
