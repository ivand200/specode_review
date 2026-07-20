from types import TracebackType
from typing import Protocol, Self

from specode_review.deadline import ReviewDeadline, review_deadline_scope
from specode_review.github import ReviewCommentGateway
from specode_review.models import ReviewRequest, ReviewResult
from specode_review.publishing import publish_review_result
from specode_review.submission import SubmissionOutcome


class ReviewService(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


class InlineReviewManager:
    """Test-only adapter for live profiles that inject in-process collaborators."""

    def __init__(
        self,
        *,
        reviewer: ReviewService,
        publisher: ReviewCommentGateway,
        review_timeout_seconds: float,
    ) -> None:
        self._reviewer = reviewer
        self._publisher = publisher
        self._review_timeout_seconds = review_timeout_seconds
        self._accepting = False

    async def __aenter__(self) -> Self:
        self._accepting = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._accepting = False

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:
        if not self._accepting:
            return SubmissionOutcome.STOPPING
        deadline = ReviewDeadline.after(self._review_timeout_seconds)
        with review_deadline_scope(deadline):
            result = self._reviewer.review(request)
            publish_review_result(
                request=request,
                result=result,
                gateway=self._publisher,
            )
        return SubmissionOutcome.ACCEPTED
