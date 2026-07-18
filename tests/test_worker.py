import asyncio
import logging
import threading
from collections.abc import Callable

import pytest

from review_agent.deadline import remaining_review_time
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import DiffRange, ReviewRequest, ReviewResult
from review_agent.worker import SingleReviewWorker, SubmissionOutcome


class UnusedReviewer:
    def review(self, request: object) -> object:
        raise AssertionError(request)


class UnusedPublisher:
    def publish(self, **values: object) -> None:
        raise AssertionError(values)


class ShutdownLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.emitted = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        if "stage=worker_shutdown" in record.getMessage():
            self.emitted.set()


class RecordingReviewer:
    def __init__(
        self,
        *,
        fail_first: Callable[[], BaseException] | None = None,
    ) -> None:
        self._fail_first = fail_first
        self.reviewed_prs: list[int] = []
        self.deadlines: list[float | None] = []
        self.reviewed = threading.Event()

    def review(self, request: ReviewRequest) -> ReviewResult:
        self.reviewed_prs.append(request.pr_number)
        self.deadlines.append(remaining_review_time(stage="test_adapter"))
        self.reviewed.set()
        if len(self.reviewed_prs) == 1 and self._fail_first is not None:
            raise self._fail_first()
        return _result(request)


class RecordingPublisher:
    def __init__(
        self,
        *,
        expected_publications: int = 1,
        fail_first: Callable[[], BaseException] | None = None,
    ) -> None:
        self._expected_publications = expected_publications
        self._fail_first = fail_first
        self.attempted_prs: list[int] = []
        self.published_prs: list[int] = []
        self.installation_ids: list[int] = []
        self.finished = threading.Event()

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, body
        self.attempted_prs.append(pr_number)
        self.installation_ids.append(installation_id)
        if len(self.attempted_prs) == 1 and self._fail_first is not None:
            raise self._fail_first()
        self.published_prs.append(pr_number)
        if len(self.published_prs) == self._expected_publications:
            self.finished.set()


class BlockingFirstReviewer(RecordingReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    def review(self, request: ReviewRequest) -> ReviewResult:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if not self.reviewed_prs:
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError
            return super().review(request)
        finally:
            with self._lock:
                self.active -= 1


class CooperativeBlockingReviewer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.reviewed_prs: list[int] = []

    def review(self, request: ReviewRequest) -> ReviewResult:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError
        self.reviewed_prs.append(request.pr_number)
        self.finished.set()
        return _result(request)


class SerialActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.timeline: list[str] = []

    def start(self, label: str) -> None:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.timeline.append(f"{label}-start")

    def finish(self, label: str) -> None:
        with self._lock:
            self.timeline.append(f"{label}-finish")
            self.active -= 1


class ActivityReviewer:
    def __init__(self, activity: SerialActivity) -> None:
        self._activity = activity
        self.reviewed_prs: list[int] = []
        self.second_started = threading.Event()

    def review(self, request: ReviewRequest) -> ReviewResult:
        label = f"review-{request.pr_number}"
        self._activity.start(label)
        try:
            self.reviewed_prs.append(request.pr_number)
            if len(self.reviewed_prs) == 2:
                self.second_started.set()
            return _result(request)
        finally:
            self._activity.finish(label)


class FirstPublicationBlocks:
    def __init__(self, activity: SerialActivity) -> None:
        self._activity = activity
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.finished = threading.Event()
        self.published_prs: list[int] = []

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, installation_id, body
        label = f"publish-{pr_number}"
        self._activity.start(label)
        try:
            if not self.published_prs:
                self.first_started.set()
                if not self.release_first.wait(timeout=5):
                    raise TimeoutError
            self.published_prs.append(pr_number)
            if len(self.published_prs) == 2:
                self.finished.set()
        finally:
            self._activity.finish(label)


class FirstReviewWaitsPastDeadline(RecordingReviewer):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release
        self.first_started = threading.Event()

    def review(self, request: ReviewRequest) -> ReviewResult:
        result = super().review(request)
        if len(self.reviewed_prs) == 1:
            self.first_started.set()
            if not self._release.wait(timeout=5):
                raise TimeoutError
        return result


def _request(pr_number: int = 17) -> ReviewRequest:
    character = "abcdef0123456789"[(pr_number - 17) % 16]
    return ReviewRequest(
        repository="octo-org/example",
        pr_number=pr_number,
        installation_id=pr_number + 100,
        base_sha="0" * 40,
        head_sha=character * 40,
        title="untrusted title",
        description="untrusted description",
    )


def _result(request: ReviewRequest) -> ReviewResult:
    return ReviewResult(
        repository=request.repository,
        pr_number=request.pr_number,
        diff_range=DiffRange(start_sha=request.base_sha, end_sha=request.head_sha),
        status="no_important_issues",
        findings=(),
    )


async def _wait_for(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


def _worker(
    *,
    reviewer: object,
    publisher: object,
    review_timeout_seconds: float = 1,
) -> SingleReviewWorker:
    return SingleReviewWorker(
        reviewer=reviewer,  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
        review_timeout_seconds=review_timeout_seconds,
    )


def test_worker_rejects_a_non_positive_review_timeout() -> None:
    with pytest.raises(ValueError, match="review timeout must be positive"):
        _worker(
            reviewer=UnusedReviewer(),
            publisher=UnusedPublisher(),
            review_timeout_seconds=0,
        )


def test_worker_accepts_only_during_its_single_lifecycle() -> None:
    reviewer = RecordingReviewer()
    publisher = RecordingPublisher()
    worker = _worker(reviewer=reviewer, publisher=publisher)

    async def exercise() -> None:
        assert worker.submit(_request()) is SubmissionOutcome.STOPPING
        async with worker:
            assert worker.submit(_request()) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.finished)
        assert worker.submit(_request()) is SubmissionOutcome.STOPPING
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            async with worker:
                pass

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17]
    assert publisher.published_prs == [17]


def test_worker_accepts_one_active_and_ten_waiting_requests() -> None:
    reviewer = BlockingFirstReviewer()
    publisher = RecordingPublisher(expected_publications=11)
    worker = _worker(reviewer=reviewer, publisher=publisher)

    async def exercise() -> None:
        async with worker:
            assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
            await _wait_for(reviewer.started)
            assert [
                worker.submit(_request(pr_number)) for pr_number in range(18, 28)
            ] == [SubmissionOutcome.ACCEPTED] * 10
            assert worker.submit(_request(28)) is SubmissionOutcome.AT_CAPACITY
            reviewer.release.set()
            await _wait_for(publisher.finished)

    try:
        asyncio.run(exercise())
    finally:
        reviewer.release.set()

    assert reviewer.maximum_active == 1
    assert reviewer.reviewed_prs == list(range(17, 28))
    assert publisher.published_prs == list(range(17, 28))


def test_worker_orders_review_and_publication_as_complete_fifo_attempts() -> None:
    activity = SerialActivity()
    reviewer = ActivityReviewer(activity)
    publisher = FirstPublicationBlocks(activity)
    worker = _worker(reviewer=reviewer, publisher=publisher)

    async def exercise() -> None:
        async with worker:
            assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
            assert worker.submit(_request(18)) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.first_started)
            assert not reviewer.second_started.is_set()
            publisher.release_first.set()
            await _wait_for(publisher.finished)

    try:
        asyncio.run(exercise())
    finally:
        publisher.release_first.set()

    assert activity.maximum_active == 1
    assert activity.timeline == [
        "review-17-start",
        "review-17-finish",
        "publish-17-start",
        "publish-17-finish",
        "review-18-start",
        "review-18-finish",
        "publish-18-start",
        "publish-18-finish",
    ]


def test_each_dequeued_request_receives_a_fresh_full_deadline() -> None:
    first_release = threading.Event()
    reviewer = FirstReviewWaitsPastDeadline(first_release)
    publisher = RecordingPublisher()
    worker = _worker(
        reviewer=reviewer,
        publisher=publisher,
        review_timeout_seconds=0.2,
    )

    async def exercise() -> None:
        release_timer = threading.Timer(0.3, first_release.set)
        release_timer.start()
        try:
            async with worker:
                assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
                await _wait_for(reviewer.first_started)
                assert worker.submit(_request(18)) is SubmissionOutcome.ACCEPTED
                await _wait_for(publisher.finished)
        finally:
            release_timer.cancel()
            first_release.set()

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17, 18]
    assert reviewer.deadlines[0] is not None
    assert reviewer.deadlines[1] is not None
    assert reviewer.deadlines[0] > 0.15
    assert reviewer.deadlines[1] > 0.15
    assert publisher.published_prs == [18]


def test_review_failure_suppresses_publication_and_preserves_normalized_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = RecordingReviewer(
        fail_first=lambda: ReviewError(
            FailureCategory.REVIEW_TOO_LARGE,
            stage="review_size",
        )
    )
    publisher = RecordingPublisher()
    worker = _worker(reviewer=reviewer, publisher=publisher)
    caplog.set_level(logging.WARNING, logger="review_agent.worker")

    async def exercise() -> None:
        async with worker:
            assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
            assert worker.submit(_request(18)) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.finished)

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17, 18]
    assert publisher.attempted_prs == [18]
    assert publisher.published_prs == [18]
    assert [record.getMessage() for record in caplog.records] == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'a' * 40} stage=review_size category=review_too_large"
    ]


@pytest.mark.parametrize(
    ("failure", "expected_category"),
    [
        pytest.param(
            lambda: TimeoutError("secret timeout detail"),
            FailureCategory.TIMEOUT,
            id="timeout",
        ),
        pytest.param(
            lambda: asyncio.CancelledError("secret cancellation detail"),
            FailureCategory.REVIEW_FAILURE,
            id="cancellation",
        ),
        pytest.param(
            lambda: RuntimeError("secret unexpected detail"),
            FailureCategory.REVIEW_FAILURE,
            id="unexpected-error",
        ),
    ],
)
def test_review_execution_failure_is_isolated_from_later_work(
    caplog: pytest.LogCaptureFixture,
    failure: Callable[[], BaseException],
    expected_category: FailureCategory,
) -> None:
    reviewer = RecordingReviewer(fail_first=failure)
    publisher = RecordingPublisher()
    worker = _worker(reviewer=reviewer, publisher=publisher)
    caplog.set_level(logging.WARNING, logger="review_agent.worker")

    async def exercise() -> None:
        async with worker:
            assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
            assert worker.submit(_request(18)) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.finished)

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17, 18]
    assert publisher.published_prs == [18]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'a' * 40} stage=review category={expected_category.value}"
    ]
    assert "secret" not in messages[0]
    assert "untrusted" not in messages[0]


def test_publication_failure_is_isolated_from_later_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = RecordingReviewer()
    publisher = RecordingPublisher(
        fail_first=lambda: RuntimeError(
            "secret exception with model text, subprocess output, and credentials"
        )
    )
    worker = _worker(reviewer=reviewer, publisher=publisher)
    caplog.set_level(logging.WARNING, logger="review_agent.worker")

    async def exercise() -> None:
        async with worker:
            assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
            assert worker.submit(_request(18)) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.finished)

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17, 18]
    assert publisher.attempted_prs == [17, 18]
    assert publisher.published_prs == [18]
    assert publisher.installation_ids == [117, 118]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'a' * 40} stage=publication category=review_failure"
    ]
    assert "secret" not in messages[0]
    assert "untrusted" not in messages[0]


def test_worker_exit_allows_the_active_attempt_to_finish_within_grace() -> None:
    reviewer = BlockingFirstReviewer()
    publisher = RecordingPublisher()
    worker = _worker(reviewer=reviewer, publisher=publisher)

    async def exercise() -> None:
        release_timer: threading.Timer | None = None
        try:
            async with worker:
                assert worker.submit(_request()) is SubmissionOutcome.ACCEPTED
                await _wait_for(reviewer.started)
                release_timer = threading.Timer(0.05, reviewer.release.set)
                release_timer.start()
        finally:
            if release_timer is not None:
                release_timer.cancel()
            reviewer.release.set()

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17]
    assert publisher.published_prs == [17]


def test_shutdown_grace_is_bounded_and_thread_cancellation_is_cooperative(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = CooperativeBlockingReviewer()
    publisher = RecordingPublisher()
    worker = _worker(
        reviewer=reviewer,
        publisher=publisher,
        review_timeout_seconds=0.05,
    )
    caplog.set_level(logging.WARNING, logger="review_agent.worker")

    async def exercise() -> None:
        async def run_lifecycle() -> None:
            async with worker:
                assert worker.submit(_request()) is SubmissionOutcome.ACCEPTED
                await _wait_for(reviewer.started)

        try:
            await asyncio.wait_for(run_lifecycle(), timeout=0.5)
            assert worker.submit(_request(18)) is SubmissionOutcome.STOPPING
            assert not reviewer.finished.is_set()
        finally:
            reviewer.release.set()
        await _wait_for(reviewer.finished)

    try:
        asyncio.run(exercise())
    finally:
        reviewer.release.set()

    assert reviewer.reviewed_prs == [17]
    assert publisher.published_prs == []
    assert [record.getMessage() for record in caplog.records] == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'a' * 40} stage=review category=timeout"
    ]


def test_shutdown_rejects_new_work_and_safely_discards_ten_waiting_requests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = BlockingFirstReviewer()
    publisher = RecordingPublisher()
    worker = _worker(reviewer=reviewer, publisher=publisher)
    shutdown_log = ShutdownLogHandler()
    worker_logger = logging.getLogger("review_agent.worker")
    worker_logger.addHandler(shutdown_log)
    caplog.set_level(logging.WARNING, logger="review_agent.worker")

    async def exercise() -> None:
        entered = asyncio.Event()
        begin_shutdown = asyncio.Event()

        async def run_worker() -> None:
            async with worker:
                entered.set()
                await begin_shutdown.wait()

        worker_lifecycle = asyncio.create_task(run_worker())
        await entered.wait()
        assert worker.submit(_request(17)) is SubmissionOutcome.ACCEPTED
        await _wait_for(reviewer.started)
        for pr_number in range(18, 28):
            request = _request(pr_number).model_copy(
                update={
                    "title": "payload text with model output",
                    "description": (
                        "pull request description with subprocess output, "
                        "credentials, and exception messages"
                    ),
                }
            )
            assert worker.submit(request) is SubmissionOutcome.ACCEPTED

        begin_shutdown.set()
        await _wait_for(shutdown_log.emitted)
        assert worker.submit(_request(28)) is SubmissionOutcome.STOPPING
        reviewer.release.set()
        await worker_lifecycle

    try:
        asyncio.run(exercise())
    finally:
        reviewer.release.set()
        worker_logger.removeHandler(shutdown_log)

    assert reviewer.reviewed_prs == [17]
    assert publisher.published_prs == [17]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example "
        f"pr_number={pr_number} "
        f"head_sha={'abcdef0123456789'[(pr_number - 17) % 16] * 40} "
        "stage=worker_shutdown category=review_failure"
        for pr_number in range(18, 28)
    ]
    observable = "\n".join(messages)
    for unsafe_text in (
        "payload text",
        "pull request description",
        "model output",
        "publication body",
        "subprocess output",
        "credentials",
        "exception messages",
    ):
        assert unsafe_text not in observable


def test_duplicate_submissions_remain_distinct_attempts() -> None:
    reviewer = RecordingReviewer()
    publisher = RecordingPublisher(expected_publications=2)
    worker = _worker(reviewer=reviewer, publisher=publisher)
    request = _request()

    async def exercise() -> None:
        async with worker:
            assert worker.submit(request) is SubmissionOutcome.ACCEPTED
            assert worker.submit(request) is SubmissionOutcome.ACCEPTED
            await _wait_for(publisher.finished)

    asyncio.run(exercise())

    assert reviewer.reviewed_prs == [17, 17]
    assert publisher.published_prs == [17, 17]
