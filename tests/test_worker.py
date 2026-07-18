import asyncio
import threading

import pytest

from review_agent.models import DiffRange, ReviewRequest, ReviewResult
from review_agent.worker import SingleReviewWorker, SubmissionOutcome


class UnusedReviewer:
    def review(self, request: object) -> object:
        raise AssertionError(request)


class UnusedPublisher:
    def publish(self, **values: object) -> None:
        raise AssertionError(values)


class CleanReviewer:
    def review(self, request: ReviewRequest) -> ReviewResult:
        return ReviewResult(
            repository=request.repository,
            pr_number=request.pr_number,
            diff_range=DiffRange(start_sha=request.base_sha, end_sha=request.head_sha),
            status="no_important_issues",
            findings=(),
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.published_prs: list[int] = []
        self.published = threading.Event()

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, installation_id, body
        self.published_prs.append(pr_number)
        self.published.set()


def _request(pr_number: int = 17) -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/example",
        pr_number=pr_number,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Feature",
        description="Description",
    )


def test_worker_rejects_a_non_positive_review_timeout() -> None:
    with pytest.raises(ValueError, match="review timeout must be positive"):
        SingleReviewWorker(
            reviewer=UnusedReviewer(),  # type: ignore[arg-type]
            publisher=UnusedPublisher(),
            review_timeout_seconds=0,
        )


def test_worker_accepts_only_during_its_single_lifecycle() -> None:
    publisher = RecordingPublisher()
    worker = SingleReviewWorker(
        reviewer=CleanReviewer(),
        publisher=publisher,
        review_timeout_seconds=1,
    )

    async def exercise() -> None:
        assert worker.submit(_request()) is SubmissionOutcome.STOPPING
        async with worker:
            assert worker.submit(_request()) is SubmissionOutcome.ACCEPTED
            assert await asyncio.to_thread(publisher.published.wait, 1)
        assert worker.submit(_request()) is SubmissionOutcome.STOPPING
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            async with worker:
                pass

    asyncio.run(exercise())

    assert publisher.published_prs == [17]
