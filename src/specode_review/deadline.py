import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from specode_review.errors import FailureCategory, ReviewError


@dataclass(frozen=True, slots=True)
class ReviewDeadline:
    expires_at: float

    @classmethod
    def after(cls, timeout_seconds: float) -> "ReviewDeadline":
        if timeout_seconds <= 0:
            message = "review timeout must be positive"
            raise ValueError(message)
        return cls(expires_at=time.monotonic() + timeout_seconds)

    def remaining(self, *, stage: str) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise ReviewError(FailureCategory.TIMEOUT, stage=stage)
        return remaining


_ACTIVE_DEADLINE: ContextVar[ReviewDeadline | None] = ContextVar(
    "specode_review_active_deadline",
    default=None,
)


@contextmanager
def review_deadline_scope(deadline: ReviewDeadline) -> Iterator[None]:
    token = _ACTIVE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _ACTIVE_DEADLINE.reset(token)


def remaining_review_time(*, stage: str) -> float | None:
    deadline = _ACTIVE_DEADLINE.get()
    if deadline is None:
        return None
    return deadline.remaining(stage=stage)
