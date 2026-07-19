import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

from review_agent.attempt import AttemptCommand, AttemptPublication, AttemptStatus
from review_agent.configuration import AttemptSettings
from review_agent.models import ReviewRequest
from review_agent.process_manager import AttemptLaunchError, ReviewProcessManager
from review_agent.resources import WORKSPACE_PREFIX, ReviewResourceManager


class RecordingSandboxResources:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_names(self) -> tuple[str, ...]:
        self.list_calls += 1
        return ()

    def remove(self, name: str) -> None:
        raise AssertionError(name)


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
        child_arguments=(
            child_executable,
            str(Path(__file__).parent / "fixtures" / "process_manager_child.py"),
            str(receipt),
            *child_arguments,
        ),
    )


def test_manager_exposes_only_the_attempt_launch_interface(tmp_path: Path) -> None:
    manager = _manager(tmp_path, tmp_path / "attempt-command.json")

    assert callable(manager.launch)
    assert not hasattr(manager, "start")
    assert not hasattr(manager, "execute")
    assert not hasattr(manager, "__aenter__")
    assert not hasattr(manager, "__aexit__")


def test_manager_returns_the_validated_outcome_from_a_real_child(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt)

    async def exercise() -> object:
        execution = await manager.launch(_request(), check_run_id=101)
        return await execution.wait()

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


def test_manager_uses_parent_assigned_attempt_id_for_retry_launch(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt)
    retry_attempt_id = "f" * 32

    async def exercise() -> object:
        execution = await manager.launch(
            _request(),
            check_run_id=101,
            attempt_id=retry_attempt_id,
        )
        return await execution.wait()

    outcome = asyncio.run(exercise())
    command = AttemptCommand.from_json_bytes(receipt.read_bytes())

    assert command.attempt_id == retry_attempt_id
    assert outcome.attempt_id == retry_attempt_id


def test_attempt_result_can_only_be_consumed_once(tmp_path: Path) -> None:
    manager = _manager(tmp_path, tmp_path / "attempt-command.json")

    async def exercise() -> None:
        execution = await manager.launch(_request(), check_run_id=101)
        await execution.wait()
        with pytest.raises(RuntimeError, match="only be consumed once"):
            await execution.wait()

    asyncio.run(exercise())


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
        execution = await manager.launch(_request(), check_run_id=101)
        return await execution.wait()

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
        execution = await manager.launch(_request(), check_run_id=101)
        return await execution.wait()

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
        execution = await manager.launch(_request(), check_run_id=101)
        return await execution.wait()

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

    async def exercise() -> AttemptLaunchError:
        with pytest.raises(AttemptLaunchError) as failure:
            await manager.launch(_request(), check_run_id=101)
        return failure.value

    error = asyncio.run(exercise())
    outcome = error.outcome

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

    async def exercise() -> AttemptLaunchError:
        with pytest.raises(AttemptLaunchError) as failure:
            await asyncio.wait_for(
                manager.launch(_request(), check_run_id=101),
                timeout=0.2,
            )
        return failure.value

    error = asyncio.run(exercise())

    assert error.outcome.failure_stage == "launch"
    assert error.outcome.publication is AttemptPublication.NOT_ATTEMPTED
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
        delivery = asyncio.create_task(manager.launch(_request(), check_run_id=101))
        await process.stdin.drain_started.wait()
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery

    asyncio.run(exercise())

    assert process.stdin.closed
    assert process.returncode is not None


def test_child_streams_are_inherited(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    receipt = tmp_path / "attempt-command.json"
    manager = _manager(tmp_path, receipt, "emit-output")

    async def exercise() -> object:
        execution = await manager.launch(_request(), check_run_id=101)
        return await execution.wait()

    outcome = asyncio.run(exercise())

    captured = capfd.readouterr()
    assert captured.out == "child stdout is inherited\n"
    assert captured.err == "child stderr is inherited\n"
    assert outcome.status is AttemptStatus.REVIEWED


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

    async def exercise() -> tuple[float, object]:
        loop = asyncio.get_running_loop()
        began = loop.time()
        execution = await manager.launch(_request(), check_run_id=101)
        outcome = await execution.wait()
        return loop.time() - began, outcome

    elapsed, outcome = asyncio.run(exercise())

    assert terminated.exists()
    assert elapsed >= 0.24
    assert outcome.status is AttemptStatus.TIMED_OUT
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

    async def exercise() -> tuple[float, object]:
        loop = asyncio.get_running_loop()
        began = loop.time()
        execution = await manager.launch(_request(), check_run_id=101)
        outcome = await execution.wait()
        return loop.time() - began, outcome

    elapsed, outcome = asyncio.run(exercise())
    direct_pid, descendant_pid = (int(value) for value in started.read_text().split())
    command = AttemptCommand.from_json_bytes(receipt.read_bytes())

    assert elapsed < 0.9
    assert outcome.status is AttemptStatus.TIMED_OUT
    workspace = tmp_path / "workspaces" / f"{WORKSPACE_PREFIX}{command.attempt_id}"
    assert not workspace.exists()
    for pid in (direct_pid, descendant_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    messages = [record.getMessage() for record in caplog.records]
    assert any("signal=SIGTERM" in message for message in messages)
    assert any("signal=SIGKILL" in message for message in messages)
    correlated = [
        message
        for message in messages
        if "signal sent" in message
    ]
    assert len(correlated) == 2
    assert all(f"attempt_id={command.attempt_id}" in message for message in correlated)
    assert all("untrusted title" not in message for message in correlated)
    assert all("untrusted description" not in message for message in correlated)
