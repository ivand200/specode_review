import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from specode_review.configuration import (
    ConfigurationError,
    ProductionPaths,
    ProductionServiceSettings,
    ReasoningEffort,
)
from specode_review.readiness import ProductionReadiness, StartupReadinessError


def _paths(tmp_path: Path) -> ProductionPaths:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    review_kit = tmp_path / "review-kit"
    review_kit.mkdir()
    return ProductionPaths(
        private_key_path=private_key,
        review_kit_path=review_kit,
        workspace_root=tmp_path / "workspaces",
    )


def _environment() -> dict[str, str]:
    return {
        "GITHUB_APP_ID": "1234",
        "GITHUB_WEBHOOK_SECRET": "a" * 32,
        "PUBLIC_WEBHOOK_URL": "https://reviews.example/webhooks/github",
        "CODEX_MODEL": "gpt-5.4",
        "OPENAI_REASONING_EFFORT": "high",
    }


def test_service_settings_expose_only_the_narrow_operator_contract(
    tmp_path: Path,
) -> None:
    settings = ProductionServiceSettings.from_environment(
        _environment(),
        paths=_paths(tmp_path),
    )

    assert settings.app_id == 1234
    assert settings.public_webhook_url == "https://reviews.example/webhooks/github"
    assert settings.max_concurrent_reviews == 3
    assert settings.log_level == "INFO"
    assert settings.codex_execution.reasoning_effort is ReasoningEffort.HIGH
    assert settings.attempt.sandbox_resources.cpus == 2
    assert settings.attempt.sandbox_resources.memory_mib == 2_048
    assert settings.attempt.sandbox_resources.pids == 256
    assert settings.attempt.workspace_root == tmp_path / "workspaces"
    assert not hasattr(settings, "repository")
    assert not hasattr(settings, "state")
    assert not hasattr(settings, "reconciliation")

    with pytest.raises(FrozenInstanceError):
        settings.max_concurrent_reviews = 4


@pytest.mark.parametrize("value", ["", "0", "6", "not-an-integer"])
def test_service_settings_reject_out_of_range_concurrency(
    tmp_path: Path,
    value: str,
) -> None:
    environment = _environment() | {"MAX_CONCURRENT_REVIEWS": value}

    with pytest.raises(ConfigurationError, match="MAX_CONCURRENT_REVIEWS"):
        ProductionServiceSettings.from_environment(environment, paths=_paths(tmp_path))


@pytest.mark.parametrize(
    "url",
    [
        "http://reviews.example/webhooks/github",
        "https://reviews.example/",
        "https://reviews.example/webhooks/github?secret=value",
    ],
)
def test_service_settings_require_a_complete_stable_https_webhook_url(
    tmp_path: Path,
    url: str,
) -> None:
    environment = _environment() | {"PUBLIC_WEBHOOK_URL": url}

    with pytest.raises(ConfigurationError, match="PUBLIC_WEBHOOK_URL"):
        ProductionServiceSettings.from_environment(environment, paths=_paths(tmp_path))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_APP_ID", "0"),
        ("GITHUB_WEBHOOK_SECRET", "short"),
        ("CODEX_MODEL", ""),
        ("CODEX_MODEL", " future-model"),
        ("OPENAI_REASONING_EFFORT", "extreme"),
        ("LOG_LEVEL", "verbose"),
    ],
)
def test_service_settings_reject_invalid_values_without_echoing_them(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    environment = _environment() | {name: value}

    with pytest.raises(ConfigurationError) as failure:
        ProductionServiceSettings.from_environment(environment, paths=_paths(tmp_path))

    assert name in str(failure.value)
    if value:
        assert value not in str(failure.value)


class ReadinessRunner:
    def __init__(self, *, fail_command: str | None = None) -> None:
        self.fail_command = fail_command
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        output_max_bytes: int,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(arguments)
        assert output_max_bytes == 1_048_576
        executable = Path(arguments[0]).name
        if executable == self.fail_command:
            return subprocess.CompletedProcess(arguments, 1, b"secret output", b"")
        if arguments[1:] == ("version",):
            stdout = b"sbx version: v0.35.0"
        elif arguments[1:] == ("--version",):
            stdout = b"codex-cli 0.144.6"
        else:
            stdout = b"ok"
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")


def test_readiness_verifies_pinned_tools_host_paths_and_review_kit(
    tmp_path: Path,
) -> None:
    runner = ReadinessRunner()
    settings = ProductionServiceSettings.from_environment(
        _environment(),
        paths=_paths(tmp_path),
    )
    readiness = ProductionReadiness(
        process_runner=runner,
        executable_resolver=lambda name: f"/usr/bin/{name}",
    )

    readiness.check(settings)

    assert runner.calls == [
        ("/usr/bin/sbx", "version"),
        ("/usr/bin/codex", "--version"),
        ("/usr/bin/git", "--version"),
        ("/usr/bin/sbx", "diagnose"),
        ("/usr/bin/sbx", "kit", "validate", str(settings.attempt.review_kit_path)),
    ]
    assert settings.attempt.workspace_root.is_dir()


def test_readiness_normalizes_external_command_failures(tmp_path: Path) -> None:
    runner = ReadinessRunner(fail_command="codex")
    settings = ProductionServiceSettings.from_environment(
        _environment(),
        paths=_paths(tmp_path),
    )
    readiness = ProductionReadiness(
        process_runner=runner,
        executable_resolver=lambda name: f"/usr/bin/{name}",
    )

    with pytest.raises(StartupReadinessError, match="codex_version") as failure:
        readiness.check(settings)

    assert "secret output" not in str(failure.value)


def test_example_environment_contains_no_obsolete_workflow_configuration() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    for removed in (
        "GITHUB_REPOSITORY=",
        "STATE_ROOT=",
        "RECONCILIATION_INTERVAL_SECONDS=",
        "SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS=",
        "SANDBOX_CPUS=",
        "WORKSPACE_ROOT=",
    ):
        assert removed not in example
