import asyncio
import logging
import subprocess
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from review_agent.configuration import (
    AttemptSettings,
    ConfigurationError,
    ProductionSettings,
    ReasoningEffort,
)
from review_agent.models import DiffRange, ReviewRequest, ReviewResult
from review_agent.production import create_production_app
from review_agent.readiness import ProductionReadiness, StartupReadinessError
from review_agent.web import create_app
from review_agent.worker import SingleReviewWorker, SubmissionOutcome


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
        "OPENAI_REASONING_EFFORT": "high",
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

    assert settings.webhook.repository == "octo-org/review-fixture"
    assert settings.attempt.app_id == 1234
    assert settings.attempt.private_key_path == tmp_path / "github-app.pem"
    assert settings.webhook.secret == "a" * 32
    assert settings.attempt.runtime.codex_execution.model == "gpt-5.4"
    assert settings.attempt.runtime.codex_execution.reasoning_effort is ReasoningEffort.HIGH
    assert settings.attempt.runtime.review_timeout_seconds == 900
    assert settings.attempt.runtime.review_limits.sandbox_resources.cpus == 2
    assert settings.attempt.runtime.review_limits.sandbox_resources.memory_mib == 4096
    assert settings.attempt.runtime.review_limits.sandbox_resources.pids == 256
    assert settings.attempt.runtime.review_limits.process_output_max_bytes == 1_048_576
    assert settings.attempt.runtime.sandbox_operation.process_output_max_bytes == 1_048_576
    assert settings.attempt.runtime.candidate_output_max_bytes == 65_536
    assert settings.attempt.runtime.sandbox_operation.cleanup_timeout_seconds == 30
    assert settings.attempt.runtime.sandbox_operation.deny_network is True
    assert settings.attempt.runtime.sandbox_name_prefix == "review-agent-"


def test_production_settings_expose_immutable_webhook_and_attempt_views(
    tmp_path: Path,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))

    assert settings.webhook.repository == "octo-org/review-fixture"
    assert settings.webhook.secret == "a" * 32
    assert settings.webhook.max_concurrent_reviews == 1
    assert settings.attempt.app_id == 1234
    assert settings.attempt.private_key_path == tmp_path / "github-app.pem"
    assert settings.attempt.review_kit_path == tmp_path / "review-kit"
    assert settings.attempt.workspace_root == tmp_path / "runtime" / "workspaces"
    assert settings.attempt.runtime.review_timeout_seconds == 900
    assert not hasattr(settings, "webhook_secret")
    assert not hasattr(settings, "runtime")

    with pytest.raises(FrozenInstanceError):
        settings.webhook.max_concurrent_reviews = 2


def test_production_settings_accept_the_maximum_review_concurrency(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["MAX_CONCURRENT_REVIEWS"] = "10"

    settings = ProductionSettings.from_environment(environment)

    assert settings.webhook.max_concurrent_reviews == 10


@pytest.mark.parametrize("value", ["", "not-an-integer", "1.5", "0", "-1", "11"])
def test_production_settings_reject_invalid_review_concurrency(
    tmp_path: Path,
    value: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["MAX_CONCURRENT_REVIEWS"] = value

    with pytest.raises(ConfigurationError, match="MAX_CONCURRENT_REVIEWS") as failure:
        ProductionSettings.from_environment(environment)

    if value:
        assert value not in str(failure.value)


def test_attempt_settings_render_a_revalidatable_allowlisted_executor_environment(
    tmp_path: Path,
) -> None:
    parent_environment = _valid_environment(tmp_path) | {
        "PATH": "/trusted/bin",
        "HOME": "/trusted/home",
        "TMPDIR": "/trusted/tmp",
        "DOCKER_HOST": "unix:///trusted/docker.sock",
        "SSL_CERT_FILE": "/trusted/cert.pem",
        "GITHUB_WEBHOOK_SECRET": "webhook-secret-must-not-cross-" + "x" * 8,
        "OPENAI_API_KEY": "raw-model-secret-must-not-cross",
        "UNRELATED_SENTINEL": "must-not-cross",
        "MAX_CONCURRENT_REVIEWS": "3",
        "NGROK_AUTHTOKEN": "parent-only-must-not-cross",
    }
    settings = ProductionSettings.from_environment(parent_environment)

    executor_environment = settings.attempt.render_executor_environment(parent_environment)

    assert AttemptSettings.from_environment(executor_environment) == settings.attempt
    assert executor_environment["PATH"] == "/trusted/bin"
    assert executor_environment["HOME"] == "/trusted/home"
    assert executor_environment["TMPDIR"] == "/trusted/tmp"
    assert executor_environment["DOCKER_HOST"] == "unix:///trusted/docker.sock"
    assert executor_environment["SSL_CERT_FILE"] == "/trusted/cert.pem"
    for excluded in (
        "GITHUB_REPOSITORY",
        "GITHUB_WEBHOOK_SECRET",
        "OPENAI_API_KEY",
        "UNRELATED_SENTINEL",
        "MAX_CONCURRENT_REVIEWS",
        "NGROK_AUTHTOKEN",
    ):
        assert excluded not in executor_environment


def test_production_settings_preserve_every_runtime_default(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)
    for name in (
        "REVIEW_TIMEOUT_SECONDS",
        "SANDBOX_CPUS",
        "SANDBOX_MEMORY_MIB",
        "SANDBOX_PIDS",
        "PROCESS_OUTPUT_MAX_BYTES",
        "CANDIDATE_OUTPUT_MAX_BYTES",
        "SANDBOX_CLEANUP_TIMEOUT_SECONDS",
        "SANDBOX_NAME_PREFIX",
    ):
        environment.pop(name)

    runtime = ProductionSettings.from_environment(environment).attempt.runtime

    assert runtime.review_timeout_seconds == 900
    assert runtime.review_limits.sandbox_resources.cpus == 2
    assert runtime.review_limits.sandbox_resources.memory_mib == 4_096
    assert runtime.review_limits.sandbox_resources.pids == 256
    assert runtime.review_limits.process_output_max_bytes == 1_048_576
    assert runtime.sandbox_operation.process_output_max_bytes == 1_048_576
    assert runtime.candidate_output_max_bytes == 65_536
    assert runtime.sandbox_operation.cleanup_timeout_seconds == 30
    assert runtime.sandbox_operation.deny_network is True
    assert runtime.sandbox_name_prefix == "review-agent-"


def test_production_settings_expose_only_one_immutable_runtime_policy(tmp_path: Path) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))

    for old_name in (
        "codex_model",
        "openai_reasoning_effort",
        "review_timeout_seconds",
        "sandbox_resources",
        "process_output_max_bytes",
        "candidate_output_max_bytes",
        "sandbox_cleanup_timeout_seconds",
        "sandbox_name_prefix",
    ):
        assert not hasattr(settings, old_name)

    with pytest.raises(FrozenInstanceError):
        settings.attempt.runtime.review_timeout_seconds = 1


@pytest.mark.parametrize(
    "configured",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
)
def test_production_settings_parse_each_supported_reasoning_effort(
    tmp_path: Path,
    configured: str,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["OPENAI_REASONING_EFFORT"] = configured

    reasoning_effort = ProductionSettings.from_environment(
        environment
    ).attempt.runtime.codex_execution.reasoning_effort

    assert isinstance(reasoning_effort, ReasoningEffort)
    assert reasoning_effort.value == configured


def test_production_settings_keep_future_codex_model_names_configurable(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["CODEX_MODEL"] = "future-model-2030"

    settings = ProductionSettings.from_environment(environment)

    assert settings.attempt.runtime.codex_execution.model == "future-model-2030"


def test_production_settings_exclude_the_webhook_secret_from_representations(
    tmp_path: Path,
) -> None:
    settings = ProductionSettings.from_environment(_valid_environment(tmp_path))

    assert settings.webhook.secret not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_REPOSITORY", "https://github.com/octo-org/review-fixture"),
        ("GITHUB_APP_ID", "0"),
        ("GITHUB_WEBHOOK_SECRET", "short"),
        ("CODEX_MODEL", ""),
        ("CODEX_MODEL", " future-model"),
        ("CODEX_MODEL", "future-model "),
        ("CODEX_MODEL", "x" * 129),
        ("OPENAI_REASONING_EFFORT", "extreme"),
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
            (sbx, "version"): b"sbx version: v0.35.0 build\n",
            (codex, "--version"): b"codex-cli 0.144.5\n",
            (git, "--version"): b"git version 2.50.1\n",
            (sbx, "diagnose"): b"Docker Sandboxes is ready\n",
            (sbx, "kit", "validate", str(settings.attempt.review_kit_path)): b"valid\n",
        }
    )
    readiness = ProductionReadiness(
        process_runner=runner,
        executable_resolver={"sbx": sbx, "codex": codex, "git": git}.get,
    )

    readiness.check(settings)

    assert settings.attempt.workspace_root.is_dir()
    process_output_max_bytes = (
        settings.attempt.runtime.sandbox_operation.process_output_max_bytes
    )
    assert runner.calls == [
        ((sbx, "version"), process_output_max_bytes),
        ((codex, "--version"), process_output_max_bytes),
        ((git, "--version"), process_output_max_bytes),
        ((sbx, "diagnose"), process_output_max_bytes),
        (
            (sbx, "kit", "validate", str(settings.attempt.review_kit_path)),
            process_output_max_bytes,
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
        (sbx, "version"): b"sbx version: v0.35.0\n",
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
    observable = (
        str(failure.value) + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    )
    assert "raw-secret" not in observable
    assert "untrusted prompt contents" not in observable
    assert observable.count("stage=review_kit_validation") == 1


class RecordingWorker:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> Self:
        self._events.append("worker_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._events.append("worker_exit")

    def submit(self, request: ReviewRequest) -> SubmissionOutcome:
        raise AssertionError(request)


class LifecycleReviewer:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def review(self, request: ReviewRequest) -> ReviewResult:
        self._events.append("review")
        return ReviewResult(
            repository=request.repository,
            pr_number=request.pr_number,
            diff_range=DiffRange(
                start_sha=request.base_sha,
                end_sha=request.head_sha,
            ),
            status="no_important_issues",
            findings=(),
        )


class BlockingLifecyclePublisher:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.started = threading.Event()
        self.release = threading.Event()

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, pr_number, installation_id, body
        self._events.append("publication_start")
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError
        self._events.append("publication_finish")


def test_application_lifespan_fails_before_accepting_traffic_when_not_ready() -> None:
    startup_failure = StartupReadinessError("review_kit_validation")
    events: list[str] = []

    def reject_startup() -> None:
        events.append("readiness")
        raise startup_failure

    app = create_app(
        repository="octo-org/example",
        webhook_secret="a" * 32,
        worker=RecordingWorker(events),
        startup_check=reject_startup,
    )

    async def start() -> None:
        async with app.router.lifespan_context(app):
            message = "startup must fail before lifespan yields"
            raise AssertionError(message)

    with pytest.raises(StartupReadinessError) as failure:
        asyncio.run(start())

    assert failure.value is startup_failure
    assert events == ["readiness"]
    assert not hasattr(app.state, "accepting_reviews")
    assert not hasattr(app.state, "review_queue")


def test_application_lifespan_releases_production_resources_on_shutdown() -> None:
    events: list[str] = []
    app = create_app(
        repository="octo-org/example",
        webhook_secret="a" * 32,
        worker=RecordingWorker(events),
        startup_check=lambda: events.append("readiness"),
        shutdown_callback=lambda: events.append("closed"),
    )

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert events == ["readiness", "worker_enter"]

    asyncio.run(run_lifespan())

    assert events == ["readiness", "worker_enter", "worker_exit", "closed"]


def test_application_lifespan_finishes_active_publication_before_resource_cleanup() -> None:
    events: list[str] = []
    publisher = BlockingLifecyclePublisher(events)
    worker = SingleReviewWorker(
        reviewer=LifecycleReviewer(events),
        publisher=publisher,
        review_timeout_seconds=1,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="a" * 32,
        worker=worker,
        startup_check=lambda: events.append("readiness"),
        shutdown_callback=lambda: events.append("closed"),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Review lifecycle ordering",
    )

    async def run_lifespan() -> None:
        release_timer: threading.Timer | None = None
        try:
            async with app.router.lifespan_context(app):
                assert worker.submit(request) is SubmissionOutcome.ACCEPTED
                assert await asyncio.to_thread(publisher.started.wait, 5)
                release_timer = threading.Timer(0.05, publisher.release.set)
                release_timer.start()
        finally:
            if release_timer is not None:
                release_timer.cancel()
            publisher.release.set()

    asyncio.run(run_lifespan())

    assert worker.submit(request) is SubmissionOutcome.STOPPING
    assert events == [
        "readiness",
        "review",
        "publication_start",
        "publication_finish",
        "closed",
    ]


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
        "OPENAI_REASONING_EFFORT",
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
    assert "sbx 0.35.0" in operator_guide
    assert "Codex CLI 0.144.5" in operator_guide
    assert "host-managed credential proxy" in operator_guide
    assert "one process" in operator_guide
    assert "RUN_FULL_LIVE_E2E=1" in live_guide
    assert "ACKNOWLEDGE_MODEL_COST=1" in live_guide
