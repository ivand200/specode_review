import os
import subprocess
import sys
from pathlib import Path

import pytest

from review_agent.attempt import (
    ATTEMPT_COMMAND_MAX_BYTES,
    AttemptCommand,
    AttemptOutcome,
    AttemptPublication,
    AttemptStatus,
)
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


def _command(*, outcome_fd: int | None = None) -> AttemptCommand:
    return AttemptCommand(
        attempt_id="0123456789abcdef0123456789abcdef",
        check_run_id=101 if outcome_fd is not None else None,
        outcome_fd=outcome_fd,
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


def _run_child_with_outcome(
    tmp_path: Path,
    *,
    mode: str = "success",
) -> tuple[subprocess.CompletedProcess[bytes], AttemptOutcome]:
    read_fd, write_fd = os.pipe()
    try:
        completed = subprocess.run(
            (
                sys.executable,
                str(Path(__file__).parent / "fixtures" / "attempt_child.py"),
                mode,
            ),
            input=_command(outcome_fd=write_fd).to_json_bytes(),
            capture_output=True,
            env=_executor_environment(tmp_path),
            pass_fds=(write_fd,),
            check=False,
        )
    finally:
        os.close(write_fd)
    try:
        document = os.read(read_fd, 8_192)
    finally:
        os.close(read_fd)
    return completed, AttemptOutcome.from_json_bytes(
        document,
        expected_attempt_id=_command().attempt_id,
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


def test_child_returns_one_trusted_outcome_after_publication_and_cleanup(
    tmp_path: Path,
) -> None:
    completed, outcome = _run_child_with_outcome(tmp_path)

    assert completed.returncode == 0
    assert completed.stdout.decode().splitlines() == ["review", "publication", "cleanup"]
    assert outcome == AttemptOutcome(
        attempt_id=_command().attempt_id,
        status=AttemptStatus.REVIEWED,
        review_status="no_important_issues",
        publication=AttemptPublication.PUBLISHED,
        failure_stage=None,
        failure_category=None,
    )


def test_child_returns_trusted_findings_status_without_finding_text(tmp_path: Path) -> None:
    completed, outcome = _run_child_with_outcome(tmp_path, mode="issues_found")

    assert completed.returncode == 0
    assert outcome.status is AttemptStatus.REVIEWED
    assert outcome.review_status == "issues_found"
    assert outcome.publication is AttemptPublication.PUBLISHED
    assert b"Bounded fixture finding" not in outcome.to_json_bytes()
    assert b"Validated fixture evidence" not in outcome.to_json_bytes()


@pytest.mark.parametrize(
    ("mode", "status", "publication", "review_status", "stage", "category"),
    [
        (
            "review_failure",
            AttemptStatus.FAILED,
            AttemptPublication.NOT_ATTEMPTED,
            None,
            "review_size",
            "review_too_large",
        ),
        (
            "timeout",
            AttemptStatus.TIMED_OUT,
            AttemptPublication.NOT_ATTEMPTED,
            None,
            "review",
            "timeout",
        ),
        (
            "publication_failure",
            AttemptStatus.FAILED,
            AttemptPublication.NOT_ATTEMPTED,
            None,
            "publication",
            "review_failure",
        ),
        (
            "publication_unknown",
            AttemptStatus.FAILED,
            AttemptPublication.UNKNOWN,
            None,
            "publication",
            "review_failure",
        ),
        (
            "cleanup_failure",
            AttemptStatus.FAILED,
            AttemptPublication.PUBLISHED,
            "no_important_issues",
            "cleanup",
            "review_failure",
        ),
    ],
)
def test_child_returns_normalized_failure_without_losing_publication_state(  # noqa: PLR0913
    tmp_path: Path,
    mode: str,
    status: AttemptStatus,
    publication: AttemptPublication,
    review_status: str | None,
    stage: str,
    category: str,
) -> None:
    completed, outcome = _run_child_with_outcome(tmp_path, mode=mode)

    assert completed.returncode != 0
    assert outcome.status is status
    assert outcome.publication is publication
    assert outcome.review_status == review_status
    assert outcome.failure_stage == stage
    assert outcome.failure_category == category
    unsafe_process_text = completed.stdout + completed.stderr
    assert b"secret" not in outcome.to_json_bytes()
    assert b"model text" not in outcome.to_json_bytes()
    assert b"subprocess output" not in outcome.to_json_bytes()
    assert outcome.to_json_bytes() not in unsafe_process_text


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
    assert (
        completed.stderr.decode()
        .strip()
        .endswith(
            "review attempt failed attempt_id=unknown stage=launch_command category=review_failure"
        )
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
    assert (
        completed.stderr.decode()
        .strip()
        .endswith(
            "review attempt failed "
            "attempt_id=0123456789abcdef0123456789abcdef "
            "stage=launch_configuration category=review_failure"
        )
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
        "review attempt failed attempt_id=unknown stage=launch_command category=review_failure"
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
