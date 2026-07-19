import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from unittest.mock import patch

import pytest

from review_agent.attempt import AttemptOutcome, AttemptPublication, AttemptStatus
from review_agent.coordinator import RetryReviewRequest, ReviewAttemptCoordinator
from review_agent.errors import FailureCategory
from review_agent.github import (
    CHECK_RUN_NAME,
    CheckRun,
    CheckRunConclusion,
    CheckRunPresentation,
    CheckRunStatus,
    GitHubError,
    GitHubOperation,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.models import ReviewRequest
from review_agent.process_manager import AttemptLaunchError, SubmissionOutcome
from review_agent.reconciliation import (
    CheckRunReconciler,
    DesiredCheckRun,
    ReconciliationTiming,
)


def _request(**overrides: object) -> ReviewRequest:
    values: dict[str, object] = {
        "repository": "octo-org/example",
        "pr_number": 17,
        "installation_id": 23,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "title": "Fix the parser",
        "description": "",
    }
    values.update(overrides)
    return ReviewRequest.model_validate(values)


def _check_run(
    identity: ReviewIdentity,
    *,
    check_run_id: int = 101,
    status: CheckRunStatus = CheckRunStatus.QUEUED,
    conclusion: CheckRunConclusion | None = None,
    retryable: bool = False,
) -> CheckRun:
    return CheckRun.model_validate(
        {
            "id": check_run_id,
            "name": CHECK_RUN_NAME,
            "head_sha": identity.head_sha,
            "external_id": identity.external_id,
            "status": status,
            "conclusion": conclusion,
            "app": {"id": 12345},
            "output": {"title": "Review queued", "summary": "Queued."},
            "actions": (
                [
                    {
                        "label": "Retry review",
                        "description": "Retry this incomplete advisory review.",
                        "identifier": "retry_review",
                    }
                ]
                if retryable
                else []
            ),
        }
    )


class _GitHub:
    def __init__(self) -> None:
        self.check_runs: list[CheckRun] = []
        self.events: list[str] = []
        self.list_error = False
        self.create_error = False
        self.get_error = False
        self.get_calls = 0
        self.review_request_error = False
        self.updates: list[CheckRunPresentation] = []
        self.review_request_value = _request()

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        del identity, installation_id
        if self.list_error:
            raise GitHubError(GitHubOperation.CHECK_RUN_LIST)
        return tuple(self.check_runs)

    def create_check_run(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> CheckRun:
        del installation_id
        if self.create_error:
            raise GitHubError(GitHubOperation.CHECK_RUN_CREATE)
        self.events.append("created")
        check_run = _check_run(identity)
        self.check_runs.append(check_run)
        return check_run

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
        return (
            check_run.app.id == 12345
            and check_run.name == CHECK_RUN_NAME
            and check_run.head_sha == identity.head_sha
            and check_run.external_id == identity.external_id
        )

    def get_check_run(self, *, check_run_id: int, installation_id: int) -> CheckRun:
        del installation_id
        self.get_calls += 1
        if self.get_error:
            raise GitHubError(GitHubOperation.CHECK_RUN_READ)
        return next(check_run for check_run in self.check_runs if check_run.id == check_run_id)

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
        del pr_number, installation_id
        if self.review_request_error:
            raise GitHubError(GitHubOperation.PULL_REQUEST_READ)
        return self.review_request_value

    def update_check_run(
        self,
        *,
        check_run_id: int,
        installation_id: int,
        presentation: CheckRunPresentation,
    ) -> CheckRun:
        del check_run_id, installation_id
        self.updates.append(presentation)
        return self.check_runs[-1]


class _Attempt:
    def __init__(self, outcome: AttemptOutcome) -> None:
        self.attempt_id = outcome.attempt_id
        self._outcome = outcome
        self.release = asyncio.Event()

    async def wait(self) -> AttemptOutcome:
        await self.release.wait()
        return self._outcome


class _Process:
    def __init__(self, github: _GitHub, attempt: _Attempt) -> None:
        self._github = github
        self._attempt = attempt
        self.launches = 0
        self.launch_attempt_ids: list[str | None] = []
        self.launch_check_run_ids: list[int] = []
        self.launch_error: AttemptLaunchError | None = None

    async def launch(
        self,
        request: ReviewRequest,
        *,
        check_run_id: int,
        attempt_id: str | None = None,
    ) -> _Attempt:
        del request
        self._github.events.append("launched")
        self.launches += 1
        self.launch_attempt_ids.append(attempt_id)
        self.launch_check_run_ids.append(check_run_id)
        if self.launch_error is not None:
            raise self.launch_error
        if attempt_id is not None:
            assert attempt_id == self._attempt.attempt_id
        return self._attempt

    def use_attempt(self, attempt: _Attempt) -> None:
        self._attempt = attempt


class _Reconciler:
    def __init__(self) -> None:
        self.desired: list[DesiredCheckRun] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def set_desired(self, desired: DesiredCheckRun) -> None:
        self.desired.append(desired)


def _outcome(  # noqa: PLR0913
    *,
    attempt_id: str = "1" * 32,
    status: AttemptStatus = AttemptStatus.REVIEWED,
    review_status: Literal["no_important_issues", "issues_found"] | None = "no_important_issues",
    publication: AttemptPublication = AttemptPublication.PUBLISHED,
    failure_stage: str | None = None,
    failure_category: FailureCategory | None = None,
) -> AttemptOutcome:
    return AttemptOutcome.model_validate(
        {
            "attempt_id": attempt_id,
            "status": status,
            "review_status": review_status,
            "publication": publication,
            "failure_stage": failure_stage,
            "failure_category": failure_category,
        }
    )


def test_new_identity_creates_check_run_before_launch_and_returns_without_waiting() -> None:
    async def exercise() -> None:
        github = _GitHub()
        attempt = _Attempt(_outcome())
        process = _Process(github, attempt)
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )

        async with coordinator:
            result = await coordinator.start(_request())
            assert result is SubmissionOutcome.ACCEPTED
            assert github.events == ["created", "launched"]
            assert [state.output_kind.value for state in reconciler.desired] == ["running"]
            attempt.release.set()

        assert [state.output_kind.value for state in reconciler.desired] == ["running", "clean"]

    asyncio.run(exercise())


def test_durable_check_run_state_prevents_duplicate_execution() -> None:
    async def exercise() -> None:
        request = _request()
        github = _GitHub()
        identity = derive_review_identity(request)
        attempt = _Attempt(_outcome())
        process = _Process(github, attempt)
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )

        async with coordinator:
            github.check_runs = [_check_run(identity)]
            assert await coordinator.start(request) is SubmissionOutcome.ALREADY_RUNNING

            github.check_runs = [
                _check_run(
                    identity,
                    status=CheckRunStatus.COMPLETED,
                    conclusion=CheckRunConclusion.SUCCESS,
                )
            ]
            assert await coordinator.start(request) is SubmissionOutcome.ALREADY_REVIEWED

        assert process.launches == 0

    asyncio.run(exercise())


def test_capacity_is_reserved_before_creation_and_released_after_completion() -> None:
    async def exercise() -> None:
        github = _GitHub()
        attempt = _Attempt(_outcome())
        process = _Process(github, attempt)
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
            max_concurrent_reviews=1,
        )

        async with coordinator:
            assert await coordinator.start(_request()) is SubmissionOutcome.ACCEPTED
            assert (
                await coordinator.start(_request(pr_number=18, head_sha="c" * 40))
                is SubmissionOutcome.AT_CAPACITY
            )
            assert process.launches == 1
            attempt.release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            second = _Attempt(_outcome(attempt_id="2" * 32))
            process.use_attempt(second)
            assert (
                await coordinator.start(_request(pr_number=18, head_sha="c" * 40))
                is SubmissionOutcome.ACCEPTED
            )
            second.release.set()

        assert process.launches == 2

    asyncio.run(exercise())


def test_completed_attempts_map_to_advisory_terminal_states() -> None:
    cases = (
        (_outcome(), "clean", 0),
        (_outcome(review_status="issues_found"), "findings", None),
        (
            _outcome(
                status=AttemptStatus.FAILED,
                review_status=None,
                publication=AttemptPublication.NOT_ATTEMPTED,
                failure_stage="review",
                failure_category=FailureCategory.REVIEW_FAILURE,
            ),
            "technical_failure",
            None,
        ),
        (
            _outcome(
                status=AttemptStatus.TIMED_OUT,
                review_status=None,
                publication=AttemptPublication.UNKNOWN,
                failure_stage="timeout",
                failure_category=FailureCategory.TIMEOUT,
            ),
            "timeout",
            None,
        ),
        (
            _outcome(
                status=AttemptStatus.FAILED,
                review_status=None,
                publication=AttemptPublication.UNKNOWN,
                failure_stage="child_outcome",
                failure_category=FailureCategory.REVIEW_FAILURE,
            ),
            "publication_unknown",
            None,
        ),
        (
            _outcome(
                status=AttemptStatus.FAILED,
                review_status="issues_found",
                publication=AttemptPublication.PUBLISHED,
                failure_stage="cleanup",
                failure_category=FailureCategory.REVIEW_FAILURE,
            ),
            "findings",
            None,
        ),
    )

    async def run_case(
        outcome: AttemptOutcome,
        expected_kind: str,
        expected_finding_count: int | None,
    ) -> None:
        github = _GitHub()
        attempt = _Attempt(outcome)
        process = _Process(github, attempt)
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )
        async with coordinator:
            assert await coordinator.start(_request()) is SubmissionOutcome.ACCEPTED
            attempt.release.set()
        terminal = reconciler.desired[-1]
        assert terminal.output_kind.value == expected_kind
        assert terminal.finding_count == expected_finding_count

    async def exercise() -> None:
        for outcome, expected_kind, expected_finding_count in cases:
            await run_case(outcome, expected_kind, expected_finding_count)

    asyncio.run(exercise())


def test_github_unavailability_never_launches_invisible_work() -> None:
    async def run_failure(*, fail_list: bool) -> None:
        github = _GitHub()
        github.list_error = fail_list
        github.create_error = not fail_list
        process = _Process(github, _Attempt(_outcome()))
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )

        async with coordinator:
            assert await coordinator.start(_request()) is SubmissionOutcome.UNAVAILABLE

        assert process.launches == 0

    async def exercise() -> None:
        await run_failure(fail_list=True)
        await run_failure(fail_list=False)

    asyncio.run(exercise())


def test_launch_failure_is_retryable_and_does_not_retain_capacity() -> None:
    async def exercise() -> None:
        failed = _outcome(
            status=AttemptStatus.FAILED,
            review_status=None,
            publication=AttemptPublication.NOT_ATTEMPTED,
            failure_stage="launch",
            failure_category=FailureCategory.REVIEW_FAILURE,
        )
        github = _GitHub()
        process = _Process(github, _Attempt(_outcome()))
        process.launch_error = AttemptLaunchError(failed)
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )

        async with coordinator:
            assert await coordinator.start(_request()) is SubmissionOutcome.UNAVAILABLE
            terminal = reconciler.desired[-1]
            assert terminal.output_kind.value == "technical_failure"
            assert terminal.failure_stage == "launch"

            github.check_runs.clear()
            process.launch_error = None
            retry = _Attempt(_outcome(attempt_id="2" * 32))
            process.use_attempt(retry)
            assert (
                await coordinator.start(_request(pr_number=18, head_sha="c" * 40))
                is SubmissionOutcome.ACCEPTED
            )
            retry.release.set()

    asyncio.run(exercise())


def test_concurrent_duplicate_submissions_cross_lookup_create_boundary_once() -> None:
    async def exercise() -> None:
        github = _GitHub()
        attempt = _Attempt(_outcome())
        process = _Process(github, attempt)
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )

        async with coordinator:
            outcomes = await asyncio.gather(
                coordinator.start(_request()),
                coordinator.start(_request()),
            )
            assert set(outcomes) == {
                SubmissionOutcome.ACCEPTED,
                SubmissionOutcome.ALREADY_RUNNING,
            }
            attempt.release.set()

        assert process.launches == 1

    asyncio.run(exercise())


def test_retry_reuses_incomplete_check_run_with_fresh_attempt_id() -> None:
    async def exercise() -> None:
        request = _request()
        identity = derive_review_identity(request)
        incomplete = _check_run(
            identity,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.NEUTRAL,
            retryable=True,
        )
        github = _GitHub()
        github.check_runs = [incomplete]
        attempt = _Attempt(_outcome(attempt_id="2" * 32))
        process = _Process(github, attempt)
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )
        retry = RetryReviewRequest(
            installation_id=request.installation_id,
            identity=identity,
            check_run=incomplete,
        )

        with patch("review_agent.coordinator.uuid.uuid4") as generate_attempt_id:
            generate_attempt_id.return_value.hex = "2" * 32
            async with coordinator:
                try:
                    assert await coordinator.retry(retry) is SubmissionOutcome.ACCEPTED
                    assert await coordinator.retry(retry) is SubmissionOutcome.ALREADY_RUNNING
                finally:
                    attempt.release.set()

        assert process.launches == 1
        assert process.launch_check_run_ids == [incomplete.id]
        assert process.launch_attempt_ids == ["2" * 32]
        assert [state.check_run_id for state in reconciler.desired] == [incomplete.id] * 3
        assert [state.output_kind.value for state in reconciler.desired] == [
            "queued",
            "running",
            "clean",
        ]
        assert [state.attempt_id for state in reconciler.desired] == ["2" * 32] * 3

    asyncio.run(exercise())


def test_retry_replay_uses_current_check_run_state_to_prevent_duplicate_work() -> None:
    async def exercise() -> None:
        request = _request()
        identity = derive_review_identity(request)
        event_check_run = _check_run(
            identity,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.NEUTRAL,
            retryable=True,
        )
        github = _GitHub()
        process = _Process(github, _Attempt(_outcome()))
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )
        retry = RetryReviewRequest(
            installation_id=request.installation_id,
            identity=identity,
            check_run=event_check_run,
        )

        async with coordinator:
            github.check_runs = [_check_run(identity, status=CheckRunStatus.IN_PROGRESS)]
            assert await coordinator.retry(retry) is SubmissionOutcome.ALREADY_RUNNING

            github.check_runs = [
                _check_run(
                    identity,
                    status=CheckRunStatus.COMPLETED,
                    conclusion=CheckRunConclusion.SUCCESS,
                )
            ]
            assert await coordinator.retry(retry) is SubmissionOutcome.ALREADY_REVIEWED

        assert process.launches == 0

    asyncio.run(exercise())


def test_retry_rejects_unowned_event_and_stale_review_revision_without_launch() -> None:
    async def exercise() -> None:
        request = _request()
        identity = derive_review_identity(request)
        owned = _check_run(
            identity,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.NEUTRAL,
            retryable=True,
        )
        unowned = owned.model_copy(update={"name": "Another App"})
        github = _GitHub()
        github.check_runs = [owned]
        process = _Process(github, _Attempt(_outcome()))
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )

        async with coordinator:
            assert (
                await coordinator.retry(
                    RetryReviewRequest(
                        installation_id=999,
                        identity=identity,
                        check_run=owned,
                    )
                )
                is SubmissionOutcome.ALREADY_REVIEWED
            )
            assert (
                await coordinator.retry(
                    RetryReviewRequest(
                        installation_id=request.installation_id,
                        identity=identity,
                        check_run=unowned,
                    )
                )
                is SubmissionOutcome.ALREADY_REVIEWED
            )
            assert github.get_calls == 0

            github.review_request_value = _request(head_sha="c" * 40)
            assert (
                await coordinator.retry(
                    RetryReviewRequest(
                        installation_id=request.installation_id,
                        identity=identity,
                        check_run=owned,
                    )
                )
                is SubmissionOutcome.ALREADY_REVIEWED
            )

        assert process.launches == 0

    asyncio.run(exercise())


def test_retry_unavailability_and_capacity_do_not_change_check_run() -> None:
    async def exercise() -> None:
        request = _request()
        identity = derive_review_identity(request)
        incomplete = _check_run(
            identity,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.NEUTRAL,
            retryable=True,
        )
        github = _GitHub()
        github.check_runs = [incomplete]
        process = _Process(github, _Attempt(_outcome()))
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )
        retry = RetryReviewRequest(
            installation_id=request.installation_id,
            identity=identity,
            check_run=incomplete,
        )

        async with coordinator:
            github.get_error = True
            assert await coordinator.retry(retry) is SubmissionOutcome.UNAVAILABLE
            github.get_error = False
            github.review_request_error = True
            assert await coordinator.retry(retry) is SubmissionOutcome.UNAVAILABLE
            github.review_request_error = False

            active = _Attempt(_outcome(attempt_id="3" * 32))
            process.use_attempt(active)
            github.check_runs.clear()
            assert (
                await coordinator.start(_request(pr_number=18, head_sha="c" * 40))
                is SubmissionOutcome.ACCEPTED
            )
            github.check_runs = [incomplete]
            assert await coordinator.retry(retry) is SubmissionOutcome.AT_CAPACITY
            active.release.set()

        assert [state.output_kind.value for state in reconciler.desired] == [
            "running",
            "clean",
        ]

    asyncio.run(exercise())


def test_retry_launch_failure_restores_retryable_terminal_state_and_capacity() -> None:
    async def exercise() -> None:
        request = _request()
        identity = derive_review_identity(request)
        incomplete = _check_run(
            identity,
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.NEUTRAL,
            retryable=True,
        )
        failed = _outcome(
            attempt_id="4" * 32,
            status=AttemptStatus.FAILED,
            review_status=None,
            publication=AttemptPublication.NOT_ATTEMPTED,
            failure_stage="launch",
            failure_category=FailureCategory.REVIEW_FAILURE,
        )
        github = _GitHub()
        github.check_runs = [incomplete]
        process = _Process(github, _Attempt(_outcome()))
        process.launch_error = AttemptLaunchError(failed)
        reconciler = _Reconciler()
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=reconciler,
            installation_id=23,
        )
        retry = RetryReviewRequest(
            installation_id=request.installation_id,
            identity=identity,
            check_run=incomplete,
        )

        with patch("review_agent.coordinator.uuid.uuid4") as generate_attempt_id:
            generate_attempt_id.return_value.hex = "4" * 32
            async with coordinator:
                assert await coordinator.retry(retry) is SubmissionOutcome.UNAVAILABLE

                process.launch_error = None
                next_attempt = _Attempt(_outcome(attempt_id="5" * 32))
                process.use_attempt(next_attempt)
                github.check_runs.clear()
                assert (
                    await coordinator.start(_request(pr_number=18, head_sha="c" * 40))
                    is SubmissionOutcome.ACCEPTED
                )
                next_attempt.release.set()

        assert [state.output_kind.value for state in reconciler.desired] == [
            "queued",
            "technical_failure",
            "running",
            "clean",
        ]
        assert reconciler.desired[0].attempt_id == "4" * 32
        assert reconciler.desired[1].attempt_id == "4" * 32

    asyncio.run(exercise())


def test_single_use_lifecycle_rejects_admission_outside_active_context() -> None:
    async def exercise() -> None:
        github = _GitHub()
        request = _request()
        identity = derive_review_identity(request)
        retry = RetryReviewRequest(
            installation_id=23,
            identity=identity,
            check_run=_check_run(
                identity,
                status=CheckRunStatus.COMPLETED,
                conclusion=CheckRunConclusion.NEUTRAL,
                retryable=True,
            ),
        )
        process = _Process(github, _Attempt(_outcome()))
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=process,
            reconciler=_Reconciler(),
            installation_id=23,
        )

        assert await coordinator.start(_request()) is SubmissionOutcome.STOPPING
        assert await coordinator.retry(retry) is SubmissionOutcome.STOPPING
        async with coordinator:
            pass
        assert await coordinator.start(_request()) is SubmissionOutcome.STOPPING
        assert await coordinator.retry(retry) is SubmissionOutcome.STOPPING

        with pytest.raises(
            RuntimeError,
            match="review attempt coordinator cannot be restarted",
        ):
            await coordinator.__aenter__()

    asyncio.run(exercise())


def test_coordinator_persists_real_latest_state_with_controlled_time(tmp_path: Path) -> None:
    async def paused_periodic_reconciliation(_: float) -> None:
        await asyncio.Event().wait()

    async def exercise() -> None:
        github = _GitHub()
        attempt = _Attempt(_outcome())
        repository_root = tmp_path / "repository-state"
        repository_root.mkdir(mode=0o700)
        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=github,
            timing=ReconciliationTiming(
                clock=lambda: datetime(2026, 7, 19, tzinfo=UTC),
                sleeper=paused_periodic_reconciliation,
            ),
        )
        coordinator = ReviewAttemptCoordinator(
            github=github,
            process=_Process(github, attempt),
            reconciler=reconciler,
            installation_id=23,
        )

        async with coordinator:
            assert await coordinator.start(_request()) is SubmissionOutcome.ACCEPTED
            attempt.release.set()

        assert [update.status.value for update in github.updates] == [
            "in_progress",
            "completed",
        ]
        assert not list((repository_root / "check-run-outbox-v1").glob("*.json"))

    asyncio.run(exercise())
