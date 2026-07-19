import asyncio
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from review_agent.github import (
    CheckRunOutputKind,
    CheckRunPresentation,
    GitHubError,
    GitHubOperation,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.models import ReviewRequest
from review_agent.reconciliation import (
    OUTBOX_DOCUMENT_MAX_BYTES,
    CheckRunReconciler,
    DesiredCheckRun,
    ReconciliationStateError,
    ReconciliationTiming,
)


def _identity() -> ReviewIdentity:
    return derive_review_identity(
        ReviewRequest(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            base_sha="a" * 40,
            head_sha="b" * 40,
            title="Fix the parser",
        )
    )


def _repository_root(tmp_path: Path) -> Path:
    repository_root = tmp_path / f"repository-v1-{'0' * 64}"
    repository_root.mkdir(mode=0o700)
    return repository_root


class _FailingGitHub:
    def update_check_run(self, **kwargs: object) -> None:
        del kwargs
        raise GitHubError(GitHubOperation.CHECK_RUN_UPDATE)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_failed_immediate_delivery_is_replayed_after_reconstruction(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository_root = _repository_root(tmp_path)
        first = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=_FailingGitHub(),
        )
        async with first:
            await first.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )

        delivered: list[dict[str, object]] = []

        class SucceedingGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                delivered.append(kwargs)

        second = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=SucceedingGitHub(),
        )
        async with second:
            pass

        assert delivered[0]["check_run_id"] == 101
        assert delivered[0]["installation_id"] == 23
        assert not list((repository_root / "check-run-outbox-v1").glob("*.json"))

    asyncio.run(exercise())


def test_retry_schedule_uses_capped_progressive_delays_without_waiting(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        attempts = 0

        class FailingGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                del kwargs
                nonlocal attempts
                attempts += 1
                raise GitHubError(GitHubOperation.CHECK_RUN_UPDATE)

        reconciler = CheckRunReconciler(
            repository_root=_repository_root(tmp_path),
            repository="octo-org/example",
            installation_id=23,
            github=FailingGitHub(),
            timing=ReconciliationTiming(clock=clock),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )
            assert attempts == 1

            clock.advance(0.999)
            await reconciler.reconcile_pending()
            assert attempts == 1

            clock.advance(0.001)
            await reconciler.reconcile_pending()
            assert attempts == 2

            for expected_attempts, delay in enumerate(
                [5, 30, 60, 300, 900, 900],
                start=3,
            ):
                clock.advance(delay)
                await reconciler.reconcile_pending()
                assert attempts == expected_attempts

    asyncio.run(exercise())


def test_newer_generation_survives_stale_delivery_and_is_delivered_last(tmp_path: Path) -> None:
    async def exercise() -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        delivered_statuses: list[str] = []

        class BlockingGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                presentation = kwargs["presentation"]
                assert isinstance(presentation, CheckRunPresentation)
                delivered_statuses.append(presentation.status.value)
                if len(delivered_statuses) == 1:
                    first_started.set()
                    assert release_first.wait(timeout=5)

        repository_root = _repository_root(tmp_path)
        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=BlockingGitHub(),
        )
        async with reconciler:
            running = asyncio.create_task(
                reconciler.set_desired(
                    DesiredCheckRun(
                        check_run_id=101,
                        identity=_identity(),
                        attempt_id="1" * 32,
                        output_kind=CheckRunOutputKind.RUNNING,
                    )
                )
            )
            assert await asyncio.to_thread(first_started.wait, 5)
            completed = asyncio.create_task(
                reconciler.set_desired(
                    DesiredCheckRun(
                        check_run_id=101,
                        identity=_identity(),
                        attempt_id="1" * 32,
                        output_kind=CheckRunOutputKind.CLEAN,
                        finding_count=0,
                    )
                )
            )
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(running, completed)

            assert delivered_statuses == ["in_progress", "completed"]
            assert not list((repository_root / "check-run-outbox-v1").glob("*.json"))

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "invalid_entry",
    ["symlink", "wrong_mode", "oversized", "repository_mismatch", "extra_field"],
)
def test_invalid_pending_entries_fail_startup_without_delivery(
    tmp_path: Path,
    invalid_entry: str,
) -> None:
    repository_root = _repository_root(tmp_path)

    async def seed_pending_entry() -> None:
        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=_FailingGitHub(),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )

    asyncio.run(seed_pending_entry())
    entry = repository_root / "check-run-outbox-v1" / "check-run-v1-101.json"
    if invalid_entry == "symlink":
        target = tmp_path / "outside.json"
        target.write_bytes(entry.read_bytes())
        target.chmod(0o600)
        entry.unlink()
        entry.symlink_to(target)
    elif invalid_entry == "wrong_mode":
        entry.chmod(0o644)
    elif invalid_entry == "oversized":
        entry.write_bytes(b"{" + b"x" * OUTBOX_DOCUMENT_MAX_BYTES + b"}")
    else:
        document = json.loads(entry.read_bytes())
        if invalid_entry == "repository_mismatch":
            document["repository"] = "octo-org/another"
        else:
            document["credential"] = "must-not-be-accepted"
        entry.write_text(json.dumps(document))

    delivered = False

    class RecordingGitHub:
        def update_check_run(self, **kwargs: object) -> None:
            del kwargs
            nonlocal delivered
            delivered = True

    async def attempt_startup() -> None:
        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=RecordingGitHub(),
        )
        async with reconciler:
            pass

    with pytest.raises(ReconciliationStateError, match="check_run_outbox"):
        asyncio.run(attempt_startup())
    assert delivered is False


def test_projection_is_persisted_before_network_delivery(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository_root = _repository_root(tmp_path)
        entry = repository_root / "check-run-outbox-v1" / "check-run-v1-101.json"
        observed_document: dict[str, object] = {}

        class InspectingGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                del kwargs
                observed_document.update(json.loads(entry.read_bytes()))

        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=InspectingGitHub(),
            timing=ReconciliationTiming(clock=_Clock()),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.CLEAN,
                    finding_count=0,
                )
            )

        assert observed_document["schema_version"] == 1
        assert observed_document["generation"] == 1
        assert observed_document["desired_status"] == "completed"
        assert observed_document["conclusion"] == "success"
        assert not entry.exists()

    asyncio.run(exercise())


def test_shutdown_makes_a_bounded_final_reconciliation_pass(tmp_path: Path) -> None:
    async def exercise() -> None:
        repository_root = _repository_root(tmp_path)
        fail = True
        attempts = 0

        class RecoveringGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                del kwargs
                nonlocal attempts
                attempts += 1
                if fail:
                    raise GitHubError(GitHubOperation.CHECK_RUN_UPDATE)

        reconciler = CheckRunReconciler(
            repository_root=repository_root,
            repository="octo-org/example",
            installation_id=23,
            github=RecoveringGitHub(),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )
            assert attempts == 1
            fail = False

        assert attempts == 2
        assert not list((repository_root / "check-run-outbox-v1").glob("*.json"))

    asyncio.run(exercise())


def test_periodic_reconciliation_delivers_due_state_while_active(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock = _Clock()
        wake_periodic = asyncio.Event()
        attempts = 0

        async def sleeper(delay: float) -> None:
            assert delay == 1
            await wake_periodic.wait()
            wake_periodic.clear()

        class FailingGitHub:
            def update_check_run(self, **kwargs: object) -> None:
                del kwargs
                nonlocal attempts
                attempts += 1
                raise GitHubError(GitHubOperation.CHECK_RUN_UPDATE)

        reconciler = CheckRunReconciler(
            repository_root=_repository_root(tmp_path),
            repository="octo-org/example",
            installation_id=23,
            github=FailingGitHub(),
            timing=ReconciliationTiming(clock=clock, sleeper=sleeper),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )
            clock.advance(1)
            wake_periodic.set()
            for _ in range(20):
                if attempts == 2:
                    break
                await asyncio.sleep(0)
            assert attempts == 2

    asyncio.run(exercise())


def test_delivery_failure_log_contains_only_normalized_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    response_secret = "raw_github_response_must_not_escape"

    class SecretGitHubError(GitHubError):
        def __str__(self) -> str:
            return response_secret

    class NormalizingGitHub:
        def update_check_run(self, **kwargs: object) -> None:
            del kwargs
            raise SecretGitHubError(GitHubOperation.CHECK_RUN_UPDATE)

    async def exercise() -> None:
        reconciler = CheckRunReconciler(
            repository_root=_repository_root(tmp_path),
            repository="octo-org/example",
            installation_id=23,
            github=NormalizingGitHub(),
        )
        async with reconciler:
            await reconciler.set_desired(
                DesiredCheckRun(
                    check_run_id=101,
                    identity=_identity(),
                    attempt_id="1" * 32,
                    output_kind=CheckRunOutputKind.RUNNING,
                )
            )

    with caplog.at_level(logging.WARNING):
        asyncio.run(exercise())

    log_output = caplog.text
    assert "operation=check_run_update" in log_output
    assert "repository=octo-org/example" in log_output
    assert "check_run_id=101" in log_output
    assert f"attempt_id={'1' * 32}" in log_output
    assert response_secret not in log_output
