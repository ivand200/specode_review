import os
import subprocess
import sys
from pathlib import Path

import pytest

from review_agent.attempt import ATTEMPT_COMMAND_MAX_BYTES, AttemptCommand
from review_agent.models import ReviewRequest


def _executor_environment(tmp_path: Path) -> dict[str, str]:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "GITHUB_APP_ID": "1234",
        "GITHUB_PRIVATE_KEY_PATH": str(private_key),
        "CODEX_MODEL": "gpt-5.4",
        "OPENAI_REASONING_EFFORT": "high",
        "REVIEW_KIT_PATH": str(review_kit),
        "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
    }


def _command() -> AttemptCommand:
    return AttemptCommand(
        attempt_id="0123456789abcdef0123456789abcdef",
        request=ReviewRequest(
            repository="octo-org/review-fixture",
            pr_number=17,
            installation_id=23,
            base_sha="a" * 40,
            head_sha="b" * 40,
            title="Untrusted title",
            description="Untrusted description",
        ),
    )


def _run_child(
    tmp_path: Path,
    *,
    command: bytes | None = None,
    environment: dict[str, str] | None = None,
    mode: str = "success",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (
            sys.executable,
            str(Path(__file__).parent / "fixtures" / "attempt_child.py"),
            mode,
        ),
        input=_command().to_json_bytes() if command is None else command,
        capture_output=True,
        env=_executor_environment(tmp_path) if environment is None else environment,
        check=False,
    )


def _run_production_child(
    tmp_path: Path,
    *,
    command: bytes,
    environment: dict[str, str] | None = None,
    arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (sys.executable, "-m", "review_agent.attempt", *arguments),
        input=command,
        capture_output=True,
        env=_executor_environment(tmp_path) if environment is None else environment,
        check=False,
    )


def test_child_exits_zero_only_after_review_publication_and_cleanup(tmp_path: Path) -> None:
    completed = _run_child(tmp_path)

    assert completed.returncode == 0
    assert completed.stdout.decode().splitlines() == ["review", "publication", "cleanup"]
    assert completed.stderr == b""


def test_child_preserves_normalized_review_failure_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    completed = _run_child(tmp_path, mode="review_failure")

    assert completed.returncode != 0
    assert completed.stdout.decode().splitlines() == ["review", "cleanup"]
    assert completed.stderr.decode().strip() == (
        "review attempt failed "
        "attempt_id=0123456789abcdef0123456789abcdef "
        "stage=review_size category=review_too_large"
    )


@pytest.mark.parametrize(
    "document",
    [
        b"malformed-untrusted-pull-request-content",
        _command().to_json_bytes() + b"{}",
        _command().to_json_bytes()[:-1] + b',"unknown":"unsafe-command-value"}',
        b"x" * (ATTEMPT_COMMAND_MAX_BYTES + 1),
    ],
)
def test_child_rejects_invalid_process_input_without_starting_review(
    tmp_path: Path,
    document: bytes,
) -> None:
    completed = _run_production_child(tmp_path, command=document)

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr.decode().strip().endswith(
        "review attempt failed "
        "attempt_id=unknown stage=launch_command category=review_failure"
    )
    assert "untrusted-pull-request-content" not in completed.stderr.decode()
    assert "unsafe-command-value" not in completed.stderr.decode()


def test_child_revalidates_executor_settings_before_starting_review(tmp_path: Path) -> None:
    environment = _executor_environment(tmp_path)
    environment.pop("CODEX_MODEL")

    completed = _run_production_child(
        tmp_path,
        command=_command().to_json_bytes(),
        environment=environment,
    )

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr.decode().strip().endswith(
        "review attempt failed "
        "attempt_id=0123456789abcdef0123456789abcdef "
        "stage=launch_configuration category=review_failure"
    )


def test_production_child_rejects_every_command_line_payload(tmp_path: Path) -> None:
    completed = _run_production_child(
        tmp_path,
        command=_command().to_json_bytes(),
        arguments=("untrusted-pull-request-title",),
    )

    assert completed.returncode != 0
    assert completed.stdout == b""
    stderr = completed.stderr.decode().strip()
    assert stderr.endswith(
        "review attempt failed "
        "attempt_id=unknown stage=launch_command category=review_failure"
    )
    assert "untrusted-pull-request-title" not in stderr


@pytest.mark.parametrize(
    ("mode", "events", "stage", "category"),
    [
        (
            "construction_failure",
            [],
            "attempt_construction",
            "review_failure",
        ),
        (
            "validation_failure",
            ["review", "cleanup"],
            "candidate_validation",
            "invalid_model_output",
        ),
        (
            "timeout",
            ["review", "cleanup"],
            "review",
            "timeout",
        ),
        (
            "publication_failure",
            ["review", "publication", "cleanup"],
            "publication",
            "review_failure",
        ),
        (
            "cleanup_failure",
            ["review", "publication", "cleanup"],
            "cleanup",
            "review_failure",
        ),
    ],
)
def test_child_failure_exit_is_normalized_and_never_skips_owned_cleanup(
    tmp_path: Path,
    mode: str,
    events: list[str],
    stage: str,
    category: str,
) -> None:
    completed = _run_child(tmp_path, mode=mode)

    assert completed.returncode != 0
    assert completed.stdout.decode().splitlines() == events
    stderr = completed.stderr.decode().strip()
    assert stderr == (
        "review attempt failed "
        "attempt_id=0123456789abcdef0123456789abcdef "
        f"stage={stage} category={category}"
    )
    for unsafe in (
        "Untrusted title",
        "Untrusted description",
        "secret",
        "model text",
        "subprocess output",
        "token",
        "private key",
        "rendered model comment",
    ):
        assert unsafe not in stderr
