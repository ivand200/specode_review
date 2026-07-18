import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import pytest

from review_agent.attempt import AttemptCommand
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


def _attempt_settings(tmp_path: Path) -> AttemptSettings:
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


def _manager(
    tmp_path: Path,
    receipt: Path,
    *child_arguments: str,
    sandbox_resources: RecordingSandboxResources | None = None,
    child_executable: str = sys.executable,
) -> ReviewProcessManager:
    settings = _attempt_settings(tmp_path)
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
