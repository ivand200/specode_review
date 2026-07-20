import asyncio
import threading
from collections import deque
from collections.abc import Callable

import pytest

from review_agent import ReviewLifecycle
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import ReviewRequest
from review_agent.review_runner import PreflightOutcome
from review_agent.submission import SubmissionOutcome


def _request(**updates: object) -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Fix the parser",
    ).model_copy(update=updates)


class ControlledRunner:
    def __init__(self) -> None:
        self.preflight_started = threading.Event()
        self.release_preflight = threading.Event()
        self.release_preflight.set()
        self.preflight_results: deque[PreflightOutcome | Exception] = deque()
        self.preflights: list[ReviewRequest] = []
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.run_failure: Exception | None = None
        self.runs: list[tuple[ReviewRequest, str]] = []

    def preflight(self, request: ReviewRequest) -> PreflightOutcome:
        self.preflights.append(request)
        self.preflight_started.set()
        if not self.release_preflight.wait(5):
            message = "controlled preflight was not released"
            raise TimeoutError(message)
        result = (
            self.preflight_results.popleft()
            if self.preflight_results
            else PreflightOutcome.READY
        )
        if isinstance(result, Exception):
            raise result
        return result

    def run(self, request: ReviewRequest, attempt_id: str) -> object:
        self.runs.append((request, attempt_id))
        self.run_started.set()
        if not self.release_run.wait(5):
            message = "controlled review was not released"
            raise TimeoutError(message)
        if self.run_failure is not None:
            raise self.run_failure
        return object()


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    message = "condition was not reached"
    raise AssertionError(message)


def test_ready_identity_is_scheduled_and_context_exit_drains_it() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        lifecycle = ReviewLifecycle(
            runner=runner,
            attempt_id_factory=lambda: "1" * 32,
        )

        async with lifecycle:
            assert await lifecycle.submit(_request()) is SubmissionOutcome.ACCEPTED
            assert await asyncio.to_thread(runner.run_started.wait, 5)
            runner.release_run.set()

        assert runner.runs == [(_request(), "1" * 32)]

    asyncio.run(exercise())


def test_duplicate_identity_collapses_during_preflight_and_running() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        runner.release_preflight.clear()
        lifecycle = ReviewLifecycle(runner=runner)
        request = _request()

        async with lifecycle:
            first = asyncio.create_task(lifecycle.submit(request))
            assert await asyncio.to_thread(runner.preflight_started.wait, 5)
            assert (
                await lifecycle.submit(
                    request.model_copy(
                        update={
                            "repository": "OCTO-ORG/EXAMPLE",
                            "installation_id": 99,
                        }
                    )
                )
                is SubmissionOutcome.ALREADY_RUNNING
            )

            runner.release_preflight.set()
            assert await first is SubmissionOutcome.ACCEPTED
            assert await asyncio.to_thread(runner.run_started.wait, 5)
            assert await lifecycle.submit(request) is SubmissionOutcome.ALREADY_RUNNING
            runner.release_run.set()

        assert len(runner.preflights) == 1
        assert len(runner.runs) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("preflight_result", "expected"),
    [
        (PreflightOutcome.ALREADY_REVIEWED, SubmissionOutcome.ALREADY_REVIEWED),
        (PreflightOutcome.NOT_AUTHORIZED, SubmissionOutcome.NOT_AUTHORIZED),
        (
            ReviewError(FailureCategory.REVIEW_FAILURE, stage="preflight"),
            SubmissionOutcome.UNAVAILABLE,
        ),
        (TimeoutError("preflight timed out"), SubmissionOutcome.UNAVAILABLE),
        (RuntimeError("unexpected provider failure"), SubmissionOutcome.UNAVAILABLE),
    ],
)
def test_preflight_disposition_releases_identity(
    preflight_result: PreflightOutcome | Exception,
    expected: SubmissionOutcome,
) -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        runner.preflight_results.extend((preflight_result, PreflightOutcome.READY))
        runner.release_run.set()

        async with ReviewLifecycle(runner=runner) as lifecycle:
            assert await lifecycle.submit(_request()) is expected
            assert await lifecycle.submit(_request()) is SubmissionOutcome.ACCEPTED

        assert len(runner.preflights) == 2
        assert len(runner.runs) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("configured_capacity", [1, 5])
def test_configured_capacity_rejects_without_queue_and_releases(
    configured_capacity: int,
) -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        lifecycle = ReviewLifecycle(
            runner=runner,
            max_concurrent_reviews=configured_capacity,
        )

        async with lifecycle:
            for offset in range(configured_capacity):
                assert (
                    await lifecycle.submit(
                        _request(pr_number=17 + offset, head_sha=f"{offset + 1:x}" * 40)
                    )
                    is SubmissionOutcome.ACCEPTED
                )
            assert (
                await lifecycle.submit(
                    _request(pr_number=90, head_sha="e" * 40)
                )
                is SubmissionOutcome.AT_CAPACITY
            )
            assert len(runner.preflights) == configured_capacity + 1
            assert len(runner.runs) == configured_capacity
            runner.release_run.set()

        assert len(runner.runs) == configured_capacity

    asyncio.run(exercise())


def test_default_capacity_is_three() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()

        async with ReviewLifecycle(runner=runner) as lifecycle:
            for offset in range(3):
                assert (
                    await lifecycle.submit(
                        _request(pr_number=17 + offset, head_sha=f"{offset + 1:x}" * 40)
                    )
                    is SubmissionOutcome.ACCEPTED
                )
            assert (
                await lifecycle.submit(_request(pr_number=20, head_sha="4" * 40))
                is SubmissionOutcome.AT_CAPACITY
            )
            runner.release_run.set()

    asyncio.run(exercise())


def test_preflight_runs_without_consuming_or_reserving_review_capacity() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        lifecycle = ReviewLifecycle(runner=runner, max_concurrent_reviews=1)

        async with lifecycle:
            assert await lifecycle.submit(_request()) is SubmissionOutcome.ACCEPTED
            assert await asyncio.to_thread(runner.run_started.wait, 5)
            runner.preflight_started.clear()
            runner.release_preflight.clear()

            second = asyncio.create_task(
                lifecycle.submit(_request(pr_number=18, head_sha="c" * 40))
            )
            assert await asyncio.to_thread(runner.preflight_started.wait, 5)
            assert not second.done()
            runner.release_preflight.set()
            assert await second is SubmissionOutcome.AT_CAPACITY
            runner.release_run.set()

    asyncio.run(exercise())


@pytest.mark.parametrize("bad_capacity", [0, 6, -1, True])
def test_capacity_must_be_an_integer_between_one_and_five(
    bad_capacity: object,
) -> None:
    with pytest.raises(ValueError, match="between 1 and 5"):
        ReviewLifecycle(
            runner=ControlledRunner(),
            max_concurrent_reviews=bad_capacity,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "run_failure",
    [
        ReviewError(FailureCategory.REVIEW_FAILURE, stage="review"),
        TimeoutError("review timed out"),
        RuntimeError("unexpected runner failure"),
    ],
)
def test_runner_failure_releases_capacity_and_identity(run_failure: Exception) -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        runner.run_failure = run_failure
        runner.release_run.set()

        async with ReviewLifecycle(runner=runner, max_concurrent_reviews=1) as lifecycle:
            assert await lifecycle.submit(_request()) is SubmissionOutcome.ACCEPTED
            await _wait_until(lambda: len(runner.runs) == 1)
            outcome = SubmissionOutcome.ALREADY_RUNNING
            for _ in range(100):
                outcome = await lifecycle.submit(_request())
                if outcome is not SubmissionOutcome.ALREADY_RUNNING:
                    break
                await asyncio.sleep(0)
            assert outcome is SubmissionOutcome.ACCEPTED

        assert len(runner.runs) == 2

    asyncio.run(exercise())


def test_shutdown_stops_preflight_promotion_and_waits_for_claim_release() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        runner.release_preflight.clear()
        lifecycle = ReviewLifecycle(runner=runner)
        await lifecycle.__aenter__()

        submission = asyncio.create_task(lifecycle.submit(_request()))
        assert await asyncio.to_thread(runner.preflight_started.wait, 5)
        shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert (
            await lifecycle.submit(_request(pr_number=18, head_sha="c" * 40))
            is SubmissionOutcome.STOPPING
        )

        runner.release_preflight.set()
        assert await submission is SubmissionOutcome.STOPPING
        await shutdown
        assert runner.runs == []

    asyncio.run(exercise())


def test_cancelled_submission_waits_for_preflight_and_releases_its_claim() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        runner.release_preflight.clear()
        lifecycle = ReviewLifecycle(runner=runner)

        async with lifecycle:
            submission = asyncio.create_task(lifecycle.submit(_request()))
            assert await asyncio.to_thread(runner.preflight_started.wait, 5)
            submission.cancel()
            await asyncio.sleep(0)
            assert not submission.done()

            runner.release_preflight.set()
            with pytest.raises(asyncio.CancelledError):
                await submission

            runner.release_run.set()
            assert (
                await lifecycle.submit(_request())
                is SubmissionOutcome.ACCEPTED
            )

        assert len(runner.preflights) == 2
        assert len(runner.runs) == 1

    asyncio.run(exercise())


def test_cancellation_cannot_abandon_work_already_promoted_to_running() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        should_cancel = True

        def cancel_submission() -> str:
            nonlocal should_cancel
            task = asyncio.current_task()
            assert task is not None
            if should_cancel:
                should_cancel = False
                task.cancel()
            return "1" * 32

        lifecycle = ReviewLifecycle(
            runner=runner,
            attempt_id_factory=cancel_submission,
        )
        await lifecycle.__aenter__()
        submission = asyncio.create_task(lifecycle.submit(_request()))
        with pytest.raises(asyncio.CancelledError):
            await submission

        assert await asyncio.to_thread(runner.run_started.wait, 5)
        assert (
            await lifecycle.submit(_request())
            is SubmissionOutcome.ALREADY_RUNNING
        )
        runner.release_run.set()
        await lifecycle.__aexit__(None, None, None)

    asyncio.run(exercise())


def test_shutdown_drains_accepted_thread_work_without_cancelling_it() -> None:
    async def exercise() -> None:
        runner = ControlledRunner()
        lifecycle = ReviewLifecycle(runner=runner)
        await lifecycle.__aenter__()
        assert await lifecycle.submit(_request()) is SubmissionOutcome.ACCEPTED
        assert await asyncio.to_thread(runner.run_started.wait, 5)

        shutdown = asyncio.create_task(lifecycle.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        runner.release_run.set()
        await shutdown
        assert len(runner.runs) == 1

    asyncio.run(exercise())


def test_lifecycle_is_one_use_and_rejects_admission_outside_context() -> None:
    async def exercise() -> None:
        lifecycle = ReviewLifecycle(runner=ControlledRunner())

        assert await lifecycle.submit(_request()) is SubmissionOutcome.STOPPING
        async with lifecycle:
            pass
        assert await lifecycle.submit(_request()) is SubmissionOutcome.STOPPING
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await lifecycle.__aenter__()

    asyncio.run(exercise())
