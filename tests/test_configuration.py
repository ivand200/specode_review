import asyncio
import logging
import subprocess
from pathlib import Path

import pytest

from review_agent.configuration import ConfigurationError, ProductionSettings
from review_agent.production import create_production_app
from review_agent.readiness import ProductionReadiness, StartupReadinessError
from review_agent.web import create_app


def _valid_environment(tmp_path: Path) -> dict[str, str]:
    private_key = tmp_path / "github-app.pem"
    private_key.write_text("test private key", encoding="utf-8")
    kit = tmp_path / "review-kit"
    kit.mkdir()
    workspace_parent = tmp_path / "runtime"
    workspace_parent.mkdir()
    return {
        "GITHUB_REPOSITORY": "octo-org/review-fixture",
        "GITHUB_APP_ID": "1234",
        "GITHUB_PRIVATE_KEY_PATH": str(private_key),
        "GITHUB_WEBHOOK_SECRET": "a" * 32,
        "CODEX_MODEL": "gpt-5.4",
        "REVIEW_KIT_PATH": str(kit),
        "WORKSPACE_ROOT": str(workspace_parent / "workspaces"),
        "REVIEW_TIMEOUT_SECONDS": "900",
        "SANDBOX_CPUS": "2",
        "SANDBOX_MEMORY_MIB": "4096",
        "SANDBOX_PIDS": "256",
        "PROCESS_OUTPUT_MAX_BYTES": "1048576",
        "CANDIDATE_OUTPUT_MAX_BYTES": "65536",
        "SANDBOX_CLEANUP_TIMEOUT_SECONDS": "30",
        "SANDBOX_NAME_PREFIX": "review-agent-",
    }


def test_production_settings_accept_the_complete_bounded_configuration(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)

    settings = ProductionSettings.from_environment(environment)

    assert settings.repository == "octo-org/review-fixture"
    assert settings.app_id == 1234
    assert settings.private_key_path == tmp_path / "github-app.pem"
    assert settings.webhook_secret == "a" * 32
    assert settings.codex_model == "gpt-5.4"
    assert settings.review_timeout_seconds == 900
    assert settings.sandbox_resources.cpus == 2
    assert settings.sandbox_resources.memory_mib == 4096
    assert settings.sandbox_resources.pids == 256
    assert settings.process_output_max_bytes == 1_048_576
    assert settings.candidate_output_max_bytes == 65_536
    assert settings.sandbox_cleanup_timeout_seconds == 30
    assert settings.sandbox_name_prefix == "review-agent-"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_REPOSITORY", "https://github.com/octo-org/review-fixture"),
        ("GITHUB_APP_ID", "0"),
        ("GITHUB_WEBHOOK_SECRET", "short"),
        ("CODEX_MODEL", ""),
        ("REVIEW_TIMEOUT_SECONDS", "0"),
        ("SANDBOX_CPUS", "0"),
        ("SANDBOX_MEMORY_MIB", "0"),
        ("SANDBOX_PIDS", "0"),
        ("PROCESS_OUTPUT_MAX_BYTES", "0"),
        ("CANDIDATE_OUTPUT_MAX_BYTES", "0"),
        ("SANDBOX_CLEANUP_TIMEOUT_SECONDS", "0"),
        ("SANDBOX_NAME_PREFIX", "unsafe_prefix"),
    ],
)
def test_production_settings_reject_invalid_values_without_echoing_them(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment[name] = value

    with pytest.raises(ConfigurationError) as failure:
        ProductionSettings.from_environment(environment)

    assert name in str(failure.value)
    if value:
        assert value not in str(failure.value)


@pytest.mark.parametrize("name", ["GITHUB_PRIVATE_KEY_PATH", "REVIEW_KIT_PATH"])
def test_production_settings_require_existing_non_symlink_secret_and_kit_paths(
    tmp_path: Path,
    name: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment[name] = str(tmp_path / "missing")

    with pytest.raises(ConfigurationError, match=name):
        ProductionSettings.from_environment(environment)


def test_production_settings_require_a_dedicated_absolute_workspace_root(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["WORKSPACE_ROOT"] = "relative/workspaces"

    with pytest.raises(ConfigurationError, match="WORKSPACE_ROOT"):
        ProductionSettings.from_environment(environment)


class RecordingReadinessProcessRunner:
    def __init__(self, responses: dict[tuple[str, ...], bytes]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        output_max_bytes: int,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, output_max_bytes))
        stdout = self.responses[arguments]
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")


def test_readiness_verifies_pinned_tools_host_and_kit_before_startup(
    tmp_path: Path,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))
    sbx = "/opt/review-agent/bin/sbx"
    codex = "/opt/review-agent/bin/codex"
    git = "/usr/bin/git"
    runner = RecordingReadinessProcessRunner(
        {
            (sbx, "version"): b"sbx version: v0.34.0 build\n",
            (codex, "--version"): b"codex-cli 0.144.5\n",
            (git, "--version"): b"git version 2.50.1\n",
            (sbx, "diagnose"): b"Docker Sandboxes is ready\n",
            (sbx, "kit", "validate", str(settings.review_kit_path)): b"valid\n",
        }
    )
    readiness = ProductionReadiness(
        process_runner=runner,
        executable_resolver={"sbx": sbx, "codex": codex, "git": git}.get,
    )

    readiness.check(settings)

    assert settings.workspace_root.is_dir()
    assert runner.calls == [
        ((sbx, "version"), settings.process_output_max_bytes),
        ((codex, "--version"), settings.process_output_max_bytes),
        ((git, "--version"), settings.process_output_max_bytes),
        ((sbx, "diagnose"), settings.process_output_max_bytes),
        (
            (sbx, "kit", "validate", str(settings.review_kit_path)),
            settings.process_output_max_bytes,
        ),
    ]


def test_readiness_rejects_an_incompatible_version_without_exposing_output(
    tmp_path: Path,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))
    sensitive_output = b"sbx version: v99.0.0 token=secret-value\n"
    runner = RecordingReadinessProcessRunner({("/bin/sbx", "version"): sensitive_output})
    readiness = ProductionReadiness(
        process_runner=runner,
        executable_resolver={"sbx": "/bin/sbx", "codex": "/bin/codex", "git": "/bin/git"}.get,
    )

    with pytest.raises(StartupReadinessError) as failure:
        readiness.check(settings)

    assert failure.value.stage == "sbx_version"
    assert "secret-value" not in str(failure.value)
    assert "99.0.0" not in str(failure.value)


def test_readiness_normalizes_invalid_kit_output_in_errors_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))
    sbx = "/bin/sbx"
    codex = "/bin/codex"
    git = "/bin/git"
    successful = {
        (sbx, "version"): b"sbx version: v0.34.0\n",
        (codex, "--version"): b"codex-cli 0.144.5\n",
        (git, "--version"): b"git version 2.50.1\n",
        (sbx, "diagnose"): b"ready\n",
    }

    def fail_invalid_kit(
        arguments: tuple[str, ...],
        output_max_bytes: int,
    ) -> subprocess.CompletedProcess[bytes]:
        del output_max_bytes
        if arguments in successful:
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=successful[arguments],
                stderr=b"",
            )
        raise subprocess.CalledProcessError(
            1,
            arguments,
            stderr=b"OPENAI_API_KEY=raw-secret untrusted prompt contents",
        )

    readiness = ProductionReadiness(
        process_runner=fail_invalid_kit,
        executable_resolver={"sbx": sbx, "codex": codex, "git": git}.get,
    )
    caplog.set_level(logging.ERROR, logger="review_agent.readiness")

    with pytest.raises(StartupReadinessError) as failure:
        readiness.check(settings)

    assert failure.value.stage == "review_kit_validation"
    observable = str(failure.value) + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "raw-secret" not in observable
    assert "untrusted prompt contents" not in observable
    assert observable.count("stage=review_kit_validation") == 1


class UnusedReviewer:
    def review(self, request: object) -> object:
        raise AssertionError(request)


class UnusedPublisher:
    def publish(self, **values: object) -> None:
        raise AssertionError(values)


def test_application_lifespan_fails_before_accepting_traffic_when_not_ready() -> None:
    startup_failure = StartupReadinessError("review_kit_validation")

    def reject_startup() -> None:
        raise startup_failure

    app = create_app(
        repository="octo-org/example",
        webhook_secret="a" * 32,
        reviewer=UnusedReviewer(),
        publisher=UnusedPublisher(),
        startup_check=reject_startup,
    )

    async def start() -> None:
        async with app.router.lifespan_context(app):
            message = "startup must fail before lifespan yields"
            raise AssertionError(message)

    with pytest.raises(StartupReadinessError) as failure:
        asyncio.run(start())

    assert failure.value is startup_failure
    assert not hasattr(app.state, "accepting_reviews")


def test_application_lifespan_releases_production_resources_on_shutdown() -> None:
    shutdown_calls: list[str] = []
    app = create_app(
        repository="octo-org/example",
        webhook_secret="a" * 32,
        reviewer=UnusedReviewer(),
        publisher=UnusedPublisher(),
        shutdown_callback=lambda: shutdown_calls.append("closed"),
    )

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.accepting_reviews is True

    asyncio.run(run_lifespan())

    assert app.state.accepting_reviews is False
    assert shutdown_calls == ["closed"]


class RejectingReadiness:
    def check(self, settings: ProductionSettings) -> None:
        del settings
        stage = "sandbox_host_capability"
        raise StartupReadinessError(stage)


def test_production_factory_fails_closed_before_constructing_runtime_dependencies(
    tmp_path: Path,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))

    with pytest.raises(StartupReadinessError, match="sandbox_host_capability"):
        create_production_app(settings=settings, readiness=RejectingReadiness())


def test_example_environment_does_not_claim_unsupported_model_or_tool_limits() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "MODEL_REQUEST_LIMIT" not in example
    assert "TOOL_CALL_LIMIT" not in example
    assert "TOTAL_TOKEN_LIMIT" not in example
    assert "COUNT_INPUT_TOKENS_BEFORE_REQUEST" not in example
    assert "OPENAI_API_KEY" not in example
    assert "100 changed files" in example
    assert "5,000 changed text lines" in example
    assert "65,536 candidate JSON bytes" in example


def test_operator_configuration_documents_the_pinned_fail_closed_runtime() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")
    operator_guide = Path("README.md").read_text(encoding="utf-8")
    live_guide = Path("tests/live/README.md").read_text(encoding="utf-8")

    for setting in (
        "CODEX_MODEL",
        "REVIEW_KIT_PATH",
        "SANDBOX_CPUS",
        "SANDBOX_MEMORY_MIB",
        "SANDBOX_PIDS",
        "PROCESS_OUTPUT_MAX_BYTES",
        "CANDIDATE_OUTPUT_MAX_BYTES",
        "SANDBOX_CLEANUP_TIMEOUT_SECONDS",
        "SANDBOX_NAME_PREFIX",
    ):
        assert setting in example
    assert "sbx 0.34.0" in operator_guide
    assert "Codex CLI 0.144.5" in operator_guide
    assert "host-managed OAuth" in operator_guide
    assert "one process" in operator_guide
    assert "RUN_FULL_LIVE_E2E=1" in live_guide
    assert "ACKNOWLEDGE_MODEL_COST=1" in live_guide
