from dataclasses import dataclass, field
from pathlib import Path

import pytest

from specode_review.verification import (
    InstallationVerifier,
    VerificationError,
    WebhookMismatchError,
    main,
)


def _environment(*, ngrok: bool = False) -> bytes:
    lines = [
        b"GITHUB_APP_ID=1234",
        b"GITHUB_WEBHOOK_SECRET=verification-secret-value-123456789",
        b"PUBLIC_WEBHOOK_URL=https://reviews.example/webhooks/github",
        b"CODEX_MODEL=gpt-5.4",
        b"OPENAI_REASONING_EFFORT=high",
        b"MAX_CONCURRENT_REVIEWS=3",
        b"LOG_LEVEL=INFO",
    ]
    if ngrok:
        lines.extend(
            (
                b"NGROK_URL=https://reviews.example",
                b"NGROK_AUTHTOKEN=ngrok-secret-sentinel",
            )
        )
    return b"\n".join(lines)


@dataclass
class ControlledVerificationHost:
    environment: bytes = field(default_factory=_environment)
    commands: list[tuple[str, ...]] = field(default_factory=list)
    sandbox_names: list[str] = field(default_factory=list)
    workspace_names: list[str] = field(default_factory=list)

    def read_file(self, path: Path, *, max_bytes: int) -> bytes:
        assert path == Path("/opt/specode-review/.env")
        assert len(self.environment) <= max_bytes
        return self.environment

    def run(self, *arguments: str) -> str:  # noqa: PLR0911 - controlled command adapter.
        self.commands.append(arguments)
        if arguments[-2:] == ("sbx", "version"):
            return "sbx version: v0.35.0\n"
        if arguments[-2:] == ("codex", "--version"):
            return "codex-cli 0.144.6\n"
        if arguments[-2:] == ("ngrok", "version"):
            return "ngrok version 3.39.1\n"
        if arguments == ("git", "--version"):
            return "git version 2.50.1\n"
        if arguments[-3:] == ("sbx", "ls", "--quiet"):
            return "\n".join(self.sandbox_names)
        if "sbx" in arguments and "create" in arguments:
            self.sandbox_names.append("specode-review-verification-probe")
        if "sbx" in arguments and "exec" in arguments:
            return "probe-ok"
        if "sbx" in arguments and "rm" in arguments:
            self.sandbox_names.remove("specode-review-verification-probe")
        return ""

    def owned_workspace_names(self) -> tuple[str, ...]:
        return tuple(self.workspace_names)

    def prepare_probe_mounts(self) -> None:
        return

    def remove_probe_mounts(self) -> None:
        return


@dataclass
class ControlledGitHubApp:
    urls: list[str]
    closed: bool = False

    def webhook_url(self) -> str:
        if len(self.urls) > 1:
            return self.urls.pop(0)
        return self.urls[0]

    def close(self) -> None:
        self.closed = True


def test_verifier_checks_the_real_contract_without_model_or_publication() -> None:
    host = ControlledVerificationHost(environment=_environment(ngrok=True))
    github = ControlledGitHubApp(["https://reviews.example/webhooks/github"])

    evidence = InstallationVerifier(
        host=host,
        github_factory=lambda _app_id: github,
        sleep=lambda _: None,
    ).verify()

    assert evidence == (
        "units",
        "local_health",
        "public_health",
        "github_app",
        "host_tools",
        "review_kit",
        "sandbox_capabilities",
        "resource_cleanup",
    )
    assert github.closed is True
    assert host.sandbox_names == []
    assert host.workspace_names == []
    assert ("systemctl", "is-enabled", "--quiet", "specode-review.service") in host.commands
    assert ("systemctl", "is-active", "--quiet", "specode-review.service") in host.commands
    assert ("systemctl", "is-enabled", "--quiet", "specode-review-ngrok.service") in host.commands
    assert ("systemctl", "is-active", "--quiet", "specode-review-ngrok.service") in host.commands
    assert (
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "10",
        "http://127.0.0.1:8000/health/live",
    ) in host.commands
    assert (
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "10",
        "https://reviews.example/health/ready",
    ) in host.commands
    flattened = "\n".join(" ".join(command) for command in host.commands)
    assert "codex exec" not in flattened
    assert "/webhooks/github" not in flattened
    assert "comment" not in flattened


def test_verifier_is_repeatable_and_rejects_stale_owned_resources() -> None:
    host = ControlledVerificationHost()
    github = ControlledGitHubApp(["https://reviews.example/webhooks/github"])
    verifier = InstallationVerifier(
        host=host,
        github_factory=lambda _app_id: github,
        sleep=lambda _: None,
    )

    assert verifier.verify() == verifier.verify()

    host.sandbox_names.append("specode-review-" + "a" * 32)
    with pytest.raises(VerificationError, match="resource_cleanup"):
        verifier.verify()


def test_verifier_waits_for_manual_webhook_correction_without_mutating_github() -> None:
    host = ControlledVerificationHost()
    github = ControlledGitHubApp(
        [
            "https://old.example/webhooks/github",
            "https://reviews.example/webhooks/github",
        ]
    )
    waits: list[float] = []

    InstallationVerifier(
        host=host,
        github_factory=lambda _app_id: github,
        sleep=waits.append,
        webhook_wait_seconds=600,
        webhook_poll_seconds=5,
    ).verify()

    assert waits == [5]
    assert all(command[0] != "gh" for command in host.commands)


def test_verifier_times_out_with_bounded_manual_webhook_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "current-secret-sentinel"
    host = ControlledVerificationHost()
    github = ControlledGitHubApp([f"https://{secret}@old.example/webhooks/github"])

    exit_code = main(
        [],
        verifier=InstallationVerifier(
            host=host,
            github_factory=lambda _app_id: github,
            sleep=lambda _: None,
            webhook_wait_seconds=0,
        ),
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert "verification failed: webhook_url_mismatch" in output.err
    assert "expected=https://reviews.example/webhooks/github" in output.err
    assert "current=(invalid)" in output.err
    assert "Update the GitHub App webhook URL manually" in output.err
    assert secret not in output.err


def test_verifier_command_emits_only_bounded_pass_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = ControlledVerificationHost()
    github = ControlledGitHubApp(["https://reviews.example/webhooks/github"])

    exit_code = main(
        [],
        verifier=InstallationVerifier(
            host=host,
            github_factory=lambda _app_id: github,
            sleep=lambda _: None,
        ),
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.err == ""
    assert output.out.splitlines() == [
        "PASS units",
        "PASS local_health",
        "PASS public_health",
        "PASS github_app",
        "PASS host_tools",
        "PASS review_kit",
        "PASS sandbox_capabilities",
        "PASS resource_cleanup",
        "verification passed",
    ]


def test_webhook_mismatch_error_never_accepts_unbounded_values() -> None:
    with pytest.raises(ValueError, match="bounded"):
        WebhookMismatchError(current="x" * 3_000, expected="https://reviews.example/webhooks/github")
