import asyncio
import logging
import os
import re
import sys
import threading
from pathlib import Path

import pytest

from review_agent.attempt import AttemptCommand, AttemptPublication, AttemptStatus
from review_agent.configuration import AttemptSettings
from review_agent.models import ReviewRequest
from review_agent.process_manager import ReviewProcessManager, SubmissionOutcome
from review_agent.resources import WORKSPACE_PREFIX, ReviewResourceManager


class RecordingSandboxResources:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_names(self) -> tuple[str, ...]:
        self.list_calls += 1
        return ()

    def remove(self, name: str) -> None:
        raise AssertionError(name)


class BlockingSandboxResources(RecordingSandboxResources):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = threading.Event()
        self.release_cleanup = threading.Event()

    def list_names(self) -> tuple[str, ...]:
        self.list_calls += 1
        self.cleanup_started.set()
        self.release_cleanup.wait()
        return ()


class FailingSandboxResources(RecordingSandboxResources):
    def list_names(self) -> tuple[str, ...]:
        message = "secret cleanup subprocess output"
        raise RuntimeError(message)


class _NeverDrainsStdin:
    def __init__(self) -> None:
        self.closed = False
        self.drain_started = asyncio.Event()

    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        self.drain_started.set()
        await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


class _NeverConsumesProcess:
    def __init__(self) -> None:
        self.stdin = _NeverDrainsStdin()
        self.pid = 999_999
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            await asyncio.Event().wait()
        return self.returncode


def _install_nonconsuming_process(
    monkeypatch: pytest.MonkeyPatch,
    process: _NeverConsumesProcess,
) -> None:
    async def spawn(*args: object, **kwargs: object) -> _NeverConsumesProcess:
        del args, kwargs
        return process

    def signal_process_group(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == process.pid
        if sent_signal == 0 and process.returncode is not None:
            raise ProcessLookupError
        if sent_signal != 0:
            process.kill()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(os, "killpg", signal_process_group)


def _attempt_settings(
    tmp_path: Path,
    *,
    review_timeout_seconds: float = 900,
    cleanup_timeout_seconds: float = 30,
) -> AttemptSettings:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir()
    return AttemptSettings.from_environment(
        {
            "GITHUB_APP_ID": "1234",
            "GITHUB_PRIVATE_KEY_PATH": str(private_key),
            "CODEX_MODEL": "gpt-5.4",
            "OPENAI_REASONING_EFFORT": "high",
            "REVIEW_KIT_PATH": str(review_kit),
            "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "REVIEW_TIMEOUT_SECONDS": str(review_timeout_seconds),
            "SANDBOX_CLEANUP_TIMEOUT_SECONDS": str(cleanup_timeout_seconds),
        }
    )


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="untrusted title",
        description="untrusted description",
    )


def _manager(  # noqa: PLR0913 - compact fixture construction seam.
    tmp_path: Path,
    receipt: Path,
    *child_arguments: str,
    sandbox_resources: RecordingSandboxResources | None = None,
    child_executable: str = sys.executable,
    max_concurrent_reviews: int = 1,
    review_timeout_seconds: float = 900,
    cleanup_timeout_seconds: float = 30,
) -> ReviewProcessManager:
    settings = _attempt_settings(
        tmp_path,
        review_timeout_seconds=review_timeout_seconds,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )
    resolved_sandbox_resources = sandbox_resources or RecordingSandboxResources()
    return ReviewProcessManager(
        attempt_settings=settings,
        resource_manager=ReviewResourceManager(
            workspace_root=settings.workspace_root,
            sandbox_prefix=settings.runtime.sandbox_name_prefix,
            sandbox_client=resolved_sandbox_resources,
        ),
        parent_environment=os.environ,
        max_concurrent_reviews=max_concurrent_reviews,
        child_arguments=(
            child_executable,
            str(Path(__file__).parent / "fixtures" / "process_manager_child.py"),
            str(receipt),
            *child_arguments,
        ),
    )


def _started_attempt_count(started: Path) -> int:
    if not started.exists():
        return 0
    return len(tuple(started.iterdir()))


async def _wait_for_started_attempts(started: Path, expected: int) -> None:
    for _ in range(500):
        if await asyncio.to_thread(_started_attempt_count, started) == expected:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"expected {expected} child attempts to start")


def test_manager_accepts_only_after_delivering_one_complete_attempt(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt)

    async def exercise() -> SubmissionOutcome:
        async with manager:
            return await manager.start(_request())

    outcome = asyncio.run(exercise())
    command = AttemptCommand.from_json_bytes(receipt.read_bytes())
    assert outcome is SubmissionOutcome.ACCEPTED
    assert command.request == _request()
    assert len(command.attempt_id) == 32
    workspace = tmp_path / "workspaces" / f"{WORKSPACE_PREFIX}{command.attempt_id}"
    assert not workspace.exists()


def test_manager_returns_the_validated_outcome_from_a_real_child(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt)

    async def exercise() -> object:
        async with manager:
            return await manager.execute(_request(), check_run_id=101)

    outcome = asyncio.run(exercise())
    command = AttemptCommand.from_json_bytes(receipt.read_bytes())

    assert outcome.attempt_id == command.attempt_id
    assert command.check_run_id == 101
    assert command.outcome_fd is not None
    assert outcome.status is AttemptStatus.REVIEWED
    assert outcome.review_status == "no_important_issues"
    assert outcome.publication is AttemptPublication.PUBLISHED
    assert outcome.failure_stage is None
    assert outcome.failure_category is None


def test_manager_preserves_published_review_when_parent_cleanup_fails(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(
        tmp_path,
        receipt,
        sandbox_resources=FailingSandboxResources(),
    )

    async def exercise() -> object:
        async with manager:
            return await manager.execute(_request(), check_run_id=101)

    outcome = asyncio.run(exercise())

    assert outcome.status is AttemptStatus.FAILED
    assert outcome.review_status == "no_important_issues"
    assert outcome.publication is AttemptPublication.PUBLISHED
    assert outcome.failure_stage == "cleanup"
    assert outcome.failure_category == "review_failure"
    assert b"secret" not in outcome.to_json_bytes()
    assert b"subprocess output" not in outcome.to_json_bytes()


@pytest.mark.parametrize(
    "mode",
    [
        "missing-outcome",
        "invalid-outcome",
        "oversized-outcome",
        "duplicated-outcome",
        "mismatched-outcome",
        "crash",
    ],
)
def test_manager_normalizes_every_untrusted_or_absent_child_outcome(
    tmp_path: Path,
    mode: str,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt, mode)

    async def exercise() -> object:
        async with manager:
            return await manager.execute(_request(), check_run_id=101)

    outcome = asyncio.run(exercise())

    assert outcome.status is AttemptStatus.FAILED
    assert outcome.review_status is None
    assert outcome.publication is AttemptPublication.UNKNOWN
    assert outcome.failure_stage == "child_outcome"
    assert outcome.failure_category == "review_failure"
    document = outcome.to_json_bytes()
    assert b"secret" not in document
    assert b"model text" not in document
    assert b"subprocess output" not in document


def test_manager_uses_unknown_publication_after_hard_termination(tmp_path: Path) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    terminated = tmp_path / "terminated"
    manager = _manager(
        tmp_path,
        receipt,
        "exit-on-term",
        str(started),
        str(terminated),
        review_timeout_seconds=0.15,
        cleanup_timeout_seconds=0.1,
    )

    async def exercise() -> object:
        async with manager:
            return await manager.execute(_request(), check_run_id=101)

    outcome = asyncio.run(exercise())

    assert terminated.exists()
    assert outcome.status is AttemptStatus.TIMED_OUT
    assert outcome.review_status is None
    assert outcome.publication is AttemptPublication.UNKNOWN
    assert outcome.failure_stage == "timeout"
    assert outcome.failure_category == "timeout"


def test_manager_returns_not_attempted_when_child_launch_fails(tmp_path: Path) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(
        tmp_path,
        receipt,
        child_executable=str(tmp_path / "missing-python"),
    )

    async def exercise() -> object:
        async with manager:
            return await manager.execute(_request(), check_run_id=101)

    outcome = asyncio.run(exercise())

    assert outcome.status is AttemptStatus.FAILED
    assert outcome.review_status is None
    assert outcome.publication is AttemptPublication.NOT_ATTEMPTED
    assert outcome.failure_stage == "launch"
    assert outcome.failure_category == "review_failure"


def test_command_delivery_is_bounded_by_the_attempt_hard_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    process = _NeverConsumesProcess()
    manager = _manager(
        tmp_path,
        receipt,
        review_timeout_seconds=0.01,
        cleanup_timeout_seconds=0.01,
    )
    _install_nonconsuming_process(monkeypatch, process)

    async def exercise() -> SubmissionOutcome:
        async with manager:
            return await asyncio.wait_for(manager.start(_request()), timeout=0.2)

    outcome = asyncio.run(exercise())

    assert outcome is SubmissionOutcome.UNAVAILABLE
    assert process.stdin.closed
    assert process.returncode is not None


def test_cancelled_command_delivery_terminates_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    process = _NeverConsumesProcess()
    manager = _manager(tmp_path, receipt)
    _install_nonconsuming_process(monkeypatch, process)

    async def exercise() -> None:
        async with manager:
            delivery = asyncio.create_task(manager.start(_request()))
            await process.stdin.drain_started.wait()
            delivery.cancel()
            with pytest.raises(asyncio.CancelledError):
                await delivery

    asyncio.run(exercise())

    assert process.stdin.closed
    assert process.returncode is not None


def test_configured_capacity_permits_three_complete_attempts(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        receipt,
        "record-start",
        str(started),
        str(release),
        max_concurrent_reviews=3,
    )

    async def exercise() -> tuple[SubmissionOutcome, ...]:
        async with manager:
            try:
                outcomes = await asyncio.gather(
                    manager.start(_request().model_copy(update={"pr_number": 17})),
                    manager.start(_request().model_copy(update={"pr_number": 18})),
                    manager.start(_request().model_copy(update={"pr_number": 19})),
                )
                await _wait_for_started_attempts(started, 3)
                return tuple(outcomes)
            finally:
                release.touch()

    outcomes = asyncio.run(exercise())
    assert outcomes == (SubmissionOutcome.ACCEPTED,) * 3
    assert len(tuple(started.iterdir())) == 3


def test_exact_active_duplicate_precedes_capacity_and_starts_no_child(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        receipt,
        "record-start",
        str(started),
        str(release),
    )

    async def exercise() -> tuple[SubmissionOutcome, SubmissionOutcome]:
        async with manager:
            accepted = await manager.start(_request())
            await _wait_for_started_attempts(started, 1)
            duplicate = await manager.start(_request())
            release.touch()
            return accepted, duplicate

    accepted, duplicate = asyncio.run(exercise())
    assert accepted is SubmissionOutcome.ACCEPTED
    assert duplicate is SubmissionOutcome.ALREADY_RUNNING
    assert len(tuple(started.iterdir())) == 1


@pytest.mark.parametrize(
    "changed_request",
    [
        pytest.param(
            _request().model_copy(update={"repository": "octo-org/other"}),
            id="repository",
        ),
        pytest.param(
            _request().model_copy(update={"pr_number": 18}),
            id="pull-request",
        ),
        pytest.param(
            _request().model_copy(update={"base_sha": "c" * 40}),
            id="base-sha",
        ),
        pytest.param(
            _request().model_copy(update={"head_sha": "d" * 40}),
            id="head-sha",
        ),
    ],
)
def test_each_active_identity_field_distinguishes_an_attempt(
    tmp_path: Path,
    changed_request: ReviewRequest,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        receipt,
        "record-start",
        str(started),
        str(release),
        max_concurrent_reviews=2,
    )

    async def exercise() -> tuple[SubmissionOutcome, SubmissionOutcome]:
        async with manager:
            try:
                outcomes = await asyncio.gather(
                    manager.start(_request()),
                    manager.start(changed_request),
                )
                await _wait_for_started_attempts(started, 2)
                return outcomes[0], outcomes[1]
            finally:
                release.touch()

    outcomes = asyncio.run(exercise())
    assert outcomes == (SubmissionOutcome.ACCEPTED,) * 2
    assert len(tuple(started.iterdir())) == 2


def test_full_capacity_retains_no_distinct_request_and_reuses_slot_after_cleanup(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        receipt,
        "record-start",
        str(started),
        str(release),
    )
    distinct_request = _request().model_copy(update={"pr_number": 18})

    async def exercise() -> tuple[SubmissionOutcome, SubmissionOutcome]:
        async with manager:
            try:
                accepted = await manager.start(_request())
                await _wait_for_started_attempts(started, 1)
                at_capacity = await manager.start(distinct_request)
                assert await asyncio.to_thread(_started_attempt_count, started) == 1
                release.touch()
                for _ in range(500):
                    retried = await manager.start(distinct_request)
                    if retried is SubmissionOutcome.ACCEPTED:
                        await _wait_for_started_attempts(started, 2)
                        return accepted, at_capacity
                    assert retried is SubmissionOutcome.AT_CAPACITY
                    await asyncio.sleep(0.01)
                pytest.fail("capacity slot was not released after cleanup")
            finally:
                release.touch()

    accepted, at_capacity = asyncio.run(exercise())
    assert accepted is SubmissionOutcome.ACCEPTED
    assert at_capacity is SubmissionOutcome.AT_CAPACITY
    assert len(tuple(started.iterdir())) == 2


def test_parent_cleanup_releases_admission_when_its_budget_expires(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    sandbox_resources = BlockingSandboxResources()
    manager = _manager(
        tmp_path,
        receipt,
        sandbox_resources=sandbox_resources,
        cleanup_timeout_seconds=0.02,
    )
    distinct_request = _request().model_copy(update={"pr_number": 18})

    async def exercise() -> tuple[SubmissionOutcome, SubmissionOutcome]:
        async with manager:
            try:
                accepted = await manager.start(_request())
                cleanup_started = await asyncio.to_thread(
                    sandbox_resources.cleanup_started.wait,
                    1,
                )
                assert cleanup_started
                for _ in range(100):
                    retried = await manager.start(distinct_request)
                    if retried is SubmissionOutcome.ACCEPTED:
                        return accepted, retried
                    assert retried is SubmissionOutcome.AT_CAPACITY
                    await asyncio.sleep(0.01)
                pytest.fail("cleanup timeout did not release admission")
            finally:
                sandbox_resources.release_cleanup.set()

    accepted, retried = asyncio.run(exercise())

    assert accepted is SubmissionOutcome.ACCEPTED
    assert retried is SubmissionOutcome.ACCEPTED


def test_exact_request_can_restart_with_a_new_attempt_after_cleanup(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        receipt,
        "record-start",
        str(started),
        str(release),
    )

    async def exercise() -> None:
        async with manager:
            try:
                assert await manager.start(_request()) is SubmissionOutcome.ACCEPTED
                await _wait_for_started_attempts(started, 1)
                release.touch()
                for _ in range(500):
                    retried = await manager.start(_request())
                    if retried is SubmissionOutcome.ACCEPTED:
                        await _wait_for_started_attempts(started, 2)
                        return
                    assert retried is SubmissionOutcome.ALREADY_RUNNING
                    await asyncio.sleep(0.01)
                pytest.fail("active key was not released after cleanup")
            finally:
                release.touch()

    asyncio.run(exercise())
    attempt_ids = tuple(path.name for path in started.iterdir())
    assert len(attempt_ids) == 2
    assert attempt_ids[0] != attempt_ids[1]


def test_manager_stops_admission_during_shutdown_and_cannot_restart(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    release = tmp_path / "release"
    manager = _manager(tmp_path, receipt, str(started), str(release))

    async def exercise() -> None:
        assert await manager.start(_request()) is SubmissionOutcome.STOPPING
        await manager.__aenter__()
        assert await manager.start(_request()) is SubmissionOutcome.ACCEPTED
        for _ in range(500):
            if started.exists():
                break
            await asyncio.sleep(0.01)
        assert started.exists()

        shutdown = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert await manager.start(_request()) is SubmissionOutcome.STOPPING
        release.touch()
        await shutdown
        assert await manager.start(_request()) is SubmissionOutcome.STOPPING
        with pytest.raises(RuntimeError, match="cannot be restarted"):
            await manager.__aenter__()

    asyncio.run(exercise())


def test_spawn_failure_is_cleaned_and_a_later_retry_can_start(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    child_executable = tmp_path / "python"
    sandbox_resources = RecordingSandboxResources()
    manager = _manager(
        tmp_path,
        receipt,
        sandbox_resources=sandbox_resources,
        child_executable=str(child_executable),
    )

    async def exercise() -> tuple[SubmissionOutcome, SubmissionOutcome]:
        async with manager:
            failed = await manager.start(_request())
            child_executable.write_text(
                f'#!/bin/sh\nexec {sys.executable} "$@"\n',
                encoding="utf-8",
            )
            child_executable.chmod(0o700)
            retried = await manager.start(_request())
            return failed, retried

    failed, retried = asyncio.run(exercise())
    assert failed is SubmissionOutcome.UNAVAILABLE
    assert retried is SubmissionOutcome.ACCEPTED
    assert AttemptCommand.from_json_bytes(receipt.read_bytes()).request == _request()
    assert sandbox_resources.list_calls == 2


def test_child_streams_and_safe_correlated_lifecycle_records_are_inherited(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt, "emit-output")
    caplog.set_level(logging.INFO, logger="review_agent.process_manager")

    async def exercise() -> None:
        async with manager:
            assert await manager.start(_request()) is SubmissionOutcome.ACCEPTED

    asyncio.run(exercise())

    captured = capfd.readouterr()
    assert captured.out == "child stdout is inherited\n"
    assert captured.err == "child stderr is inherited\n"
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    matches = [re.search(r"attempt_id=([0-9a-f]{32})", message) for message in messages]
    assert all(match is not None for match in matches)
    attempt_ids = [match.group(1) for match in matches if match is not None]
    assert attempt_ids[0] == attempt_ids[1]
    assert "pid=" in messages[0]
    assert "outcome=success" in messages[1]
    for message in messages:
        assert "repository=octo-org/example" in message
        assert "pr_number=17" in message
        assert f"base_sha={'a' * 40}" in message
        assert f"head_sha={'b' * 40}" in message
        assert "untrusted title" not in message
        assert "untrusted description" not in message


def test_overlong_attempt_gets_sigterm_after_review_and_cleanup_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    terminated = tmp_path / "terminated"
    manager = _manager(
        tmp_path,
        receipt,
        "exit-on-term",
        str(started),
        str(terminated),
        review_timeout_seconds=0.15,
        cleanup_timeout_seconds=0.1,
    )
    caplog.set_level(logging.INFO, logger="review_agent.process_manager")

    async def exercise() -> float:
        loop = asyncio.get_running_loop()
        began = loop.time()
        async with manager:
            assert await manager.start(_request()) is SubmissionOutcome.ACCEPTED
            for _ in range(100):
                if started.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("child did not start")
        return loop.time() - began

    elapsed = asyncio.run(exercise())

    assert terminated.exists()
    assert elapsed >= 0.24
    assert not any("signal=SIGKILL" in record.getMessage() for record in caplog.records)


def test_sigterm_ignoring_attempt_and_descendant_are_hard_stopped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    started = tmp_path / "started"
    manager = _manager(
        tmp_path,
        receipt,
        "ignore-term-group",
        str(started),
        review_timeout_seconds=0.3,
        cleanup_timeout_seconds=0.1,
    )
    caplog.set_level(logging.INFO, logger="review_agent.process_manager")

    async def exercise() -> float:
        loop = asyncio.get_running_loop()
        began = loop.time()
        async with manager:
            assert await manager.start(_request()) is SubmissionOutcome.ACCEPTED
            for _ in range(100):
                if started.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("process group did not start")
        return loop.time() - began

    elapsed = asyncio.run(exercise())
    direct_pid, descendant_pid = (int(value) for value in started.read_text().split())
    command = AttemptCommand.from_json_bytes(receipt.read_bytes())

    assert elapsed < 0.9
    workspace = tmp_path / "workspaces" / f"{WORKSPACE_PREFIX}{command.attempt_id}"
    assert not workspace.exists()
    for pid in (direct_pid, descendant_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    messages = [record.getMessage() for record in caplog.records]
    assert any("signal=SIGTERM" in message for message in messages)
    assert any("signal=SIGKILL" in message for message in messages)
    assert any("stage=timeout" in message for message in messages)
    assert any("outcome=hard_timeout child_status=signal_9" in message for message in messages)
    assert any("stage=cleanup outcome=success" in message for message in messages)
    correlated = [
        message
        for message in messages
        if "hard timeout" in message
        or "signal sent" in message
        or "process exited" in message
        or "cleanup completed" in message
    ]
    assert len(correlated) == 5
    assert all(f"attempt_id={command.attempt_id}" in message for message in correlated)
    assert all("untrusted title" not in message for message in correlated)
    assert all("untrusted description" not in message for message in correlated)
