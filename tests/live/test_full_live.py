import hashlib
import hmac
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.core import (
    GitHubRepository,
    ReviewContext,
    Reviewer,
    ReviewLimits,
    SandboxResourceLimits,
)
from review_agent.github import GitHubAppClient
from review_agent.process import ProcessOptions, _run_bounded_process
from review_agent.publishing import ReviewPublisher
from review_agent.readiness import ProductionReadiness
from review_agent.sandbox import (
    CodexSandboxRunner,
    DockerSandboxClient,
    DockerSandboxConfig,
)
from review_agent.web import create_app


class RecordingPublisher:
    def __init__(self, publisher: ReviewPublisher, resources_path: Path) -> None:
        self._publisher = publisher
        self._resources_path = resources_path
        self.body: str | None = None
        self.published = threading.Event()

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        self._publisher.publish(
            repository=repository,
            pr_number=pr_number,
            installation_id=installation_id,
            body=body,
        )
        self.body = body
        self._resources_path.parent.mkdir(parents=True, exist_ok=True)
        with self._resources_path.open("a", encoding="utf-8") as resources:
            resources.write(
                json.dumps(
                    {
                        "kind": "github_pull_request_comment",
                        "repository": repository,
                        "pr_number": pr_number,
                        "cleanup": "delete the checkpoint C automated review comment",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        self.published.set()


class RecordingRunner:
    def __init__(
        self,
        runner: CodexSandboxRunner,
        *,
        repository_control_markers: Mapping[Path, str],
    ) -> None:
        self._runner = runner
        self._repository_control_markers = repository_control_markers
        self.context: ReviewContext | None = None
        self.host_checkout_unchanged = False
        self.repository_controls_present = False

    def run(self, context: ReviewContext) -> object:
        self.context = context
        self.repository_controls_present = all(
            marker in (context.checkout / relative_path).read_text(encoding="utf-8")
            for relative_path, marker in self._repository_control_markers.items()
        )
        if not self.repository_controls_present:
            message = "checkpoint C repository control fixtures are missing"
            raise RuntimeError(message)
        before = self._checkout_identity(context.checkout)
        candidate = self._runner.run(context)
        self.host_checkout_unchanged = self._checkout_identity(context.checkout) == before
        return candidate

    @staticmethod
    def _checkout_identity(checkout: Path) -> tuple[str, str]:
        head = subprocess.run(
            ("git", "-C", str(checkout), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return head, status


class ReviewFailureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.failed = threading.Event()
        self.message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "review_agent.web" and record.getMessage().startswith(
            "review failed "
        ):
            self.message = record.getMessage()
            self.failed.set()


class RecordingProcessRunner:
    def __init__(self) -> None:
        self.operation = "not_started"
        self.failed_operation = "none"
        self.error = "none"
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]:
        command = Path(arguments[0]).name
        if len(arguments) > 1:
            command = f"{command} {arguments[1]}"
        if "--" in arguments:
            inner_index = arguments.index("--") + 1
            if inner_index < len(arguments):
                command = f"{command} {arguments[inner_index]}"
        self.operation = f"{options.stage}:{command}"
        self.calls.append(arguments)
        try:
            return _run_bounded_process(arguments, options)
        except subprocess.CalledProcessError as error:
            if self.error == "none":
                self.failed_operation = self.operation
                self.error = f"CalledProcessError({error.returncode})"
            raise
        except Exception as error:
            if self.error == "none":
                self.failed_operation = self.operation
                self.error = type(error).__name__
            raise

    def diagnostics(self) -> str:
        return f"operation={self.failed_operation} error={self.error}"

    def created_with_kit(self, kit: Path) -> bool:
        return any(
            len(arguments) > 1
            and arguments[1] == "create"
            and "--kit" in arguments
            and arguments[arguments.index("--kit") + 1] == str(kit)
            for arguments in self.calls
        )

    def codex_ignored_repository_configuration(self) -> bool:
        for arguments in self.calls:
            if "--" not in arguments:
                continue
            command = arguments[arguments.index("--") + 1 :]
            if command[:2] != ("codex", "exec"):
                continue
            return "--ignore-user-config" in command and "--ignore-rules" in command
        return False


class VerifyingCodexSandboxClient:
    def __init__(self, client: DockerSandboxClient) -> None:
        self._client = client
        self.loaded_kit: Path | None = None
        self.forbidden_network_denied = False

    def create_codex(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        kit: Path,
        resources: SandboxResourceLimits,
    ) -> None:
        self._client.create_codex(
            name=name,
            control=control,
            checkout=checkout,
            kit=kit,
            resources=resources,
        )
        self.loaded_kit = kit
        self._client.execute(
            name=name,
            command=(
                "sh",
                "-c",
                "command -v curl >/dev/null || exit 74; "
                "if curl -fsS --connect-timeout 5 --max-time 10 "
                "https://github.com/ >/dev/null; then exit 73; fi",
            ),
            workdir=None,
            process_limit=resources.pids,
        )
        self.forbidden_network_denied = True

    def execute(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        workdir: str | None,
        process_limit: int,
    ) -> bytes:
        return self._client.execute(
            name=name,
            command=command,
            workdir=workdir,
            process_limit=process_limit,
        )

    def remove(self, name: str) -> None:
        self._client.remove(name)

    def list_names(self) -> tuple[str, ...]:
        return self._client.list_names()


@contextmanager
def _watch_review_failures() -> Iterator[ReviewFailureHandler]:
    failure_handler = ReviewFailureHandler()
    worker_logger = logging.getLogger("review_agent.web")
    worker_logger.addHandler(failure_handler)
    try:
        yield failure_handler
    finally:
        worker_logger.removeHandler(failure_handler)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for checkpoint C")
    return value


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    host, port = server_socket.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [server_socket]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        message = "checkpoint C server did not start"
        raise RuntimeError(message)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        server_socket.close()


def _send_signed_webhook(url: str, payload: dict[str, object], secret: str) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{url}/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": signature,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode()


def _wait_for_publication_or_failure(
    publisher: RecordingPublisher,
    failure_handler: ReviewFailureHandler,
    *,
    timeout_seconds: float,
    diagnostics: Callable[[], str],
) -> None:
    wait_deadline = time.monotonic() + timeout_seconds
    while not publisher.published.wait(timeout=0.1):
        if failure_handler.failed.is_set():
            pytest.fail(
                f"checkpoint C worker failed: {failure_handler.message}; {diagnostics()}",
                pytrace=False,
            )
        if time.monotonic() >= wait_deadline:
            pytest.fail("checkpoint C publication timed out", pytrace=False)


def _assert_checkpoint_isolation(
    *,
    runner: RecordingRunner,
    sandbox_client: VerifyingCodexSandboxClient,
    process_runner: RecordingProcessRunner,
    settings: ProductionSettings,
) -> None:
    assert runner.host_checkout_unchanged
    assert runner.repository_controls_present
    assert sandbox_client.loaded_kit == settings.review_kit_path
    assert sandbox_client.forbidden_network_denied
    assert process_runner.created_with_kit(settings.review_kit_path)
    assert process_runner.codex_ignored_repository_configuration()
    assert list(settings.workspace_root.iterdir()) == []
    assert not any(
        name.startswith(settings.sandbox_name_prefix) for name in sandbox_client.list_names()
    )


def _assert_no_secret_leakage(
    observable_text: str,
    settings: ProductionSettings,
) -> None:
    sensitive_values = [
        settings.webhook_secret,
        settings.private_key_path.read_text(encoding="utf-8"),
    ]
    raw_openai_key = os.environ.get("OPENAI_API_KEY")
    if raw_openai_key:
        sensitive_values.append(raw_openai_key)
    assert all(secret not in observable_text for secret in sensitive_values)
    assert "REVIEW_AGENT_GITHUB_TOKEN" not in observable_text
    assert "OPENAI_API_KEY" not in observable_text


@pytest.mark.live_full
def test_full_live_signed_webhook_reviews_in_sandbox_and_publishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    if os.environ.get("RUN_FULL_LIVE_E2E") != "1":
        pytest.skip("set RUN_FULL_LIVE_E2E=1 to enable checkpoint C")
    if os.environ.get("ACKNOWLEDGE_MODEL_COST") != "1":
        pytest.fail("set ACKNOWLEDGE_MODEL_COST=1 to approve the checkpoint C model call")

    settings = ProductionSettings.from_environment(os.environ)
    repository = _required_environment("E2E_GITHUB_REPOSITORY")
    if repository != settings.repository or "test" not in repository.casefold():
        pytest.fail("checkpoint C requires the configured dedicated test repository")
    expected_finding = _required_environment("E2E_EXPECTED_FINDING")
    forbidden_instruction = _required_environment("E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT")
    forbidden_config = _required_environment("E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT")
    resources_path = Path(_required_environment("E2E_CREATED_RESOURCES_PATH"))
    if settings.workspace_root.resolve().is_relative_to(Path.cwd().resolve()):
        pytest.fail("checkpoint C workspace must not be inside the project working copy")
    if settings.workspace_root.exists() and any(settings.workspace_root.iterdir()):
        pytest.fail("checkpoint C requires an empty dedicated workspace root")

    ProductionReadiness().check(settings)
    github = GitHubAppClient(
        repository=settings.repository,
        app_id=settings.app_id,
        private_key_path=settings.private_key_path,
    )
    installation_id = github.repository_installation_id()
    accepted_request = github.review_request(
        pr_number=int(_required_environment("E2E_GITHUB_PR_NUMBER")),
        installation_id=installation_id,
    )
    process_runner = RecordingProcessRunner()
    sandbox_client = VerifyingCodexSandboxClient(
        DockerSandboxClient(
            process_runner=process_runner,
            config=DockerSandboxConfig(
                process_output_max_bytes=settings.process_output_max_bytes,
                cleanup_timeout_seconds=settings.sandbox_cleanup_timeout_seconds,
            ),
        )
    )
    runner = RecordingRunner(
        CodexSandboxRunner(
            client=sandbox_client,
            sandbox_prefix=settings.sandbox_name_prefix,
            kit=settings.review_kit_path,
            model=settings.codex_model,
            candidate_output_max_bytes=settings.candidate_output_max_bytes,
        ),
        repository_control_markers={
            Path("AGENTS.md"): forbidden_instruction,
            Path(".codex/config.toml"): forbidden_config,
        },
    )
    reviewer = Reviewer(
        repository=settings.repository,
        workspace_root=settings.workspace_root,
        runner=runner,
        source_repository=GitHubRepository(credentials=github),
        limits=ReviewLimits(
            process_output_max_bytes=settings.process_output_max_bytes,
            sandbox_resources=settings.sandbox_resources,
        ),
    )
    publisher = RecordingPublisher(github, resources_path)
    app = create_app(
        repository=settings.repository,
        webhook_secret=settings.webhook_secret,
        reviewer=reviewer,
        publisher=publisher,
        review_timeout_seconds=settings.review_timeout_seconds,
        shutdown_callback=github.close,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": installation_id},
        "repository": {"full_name": accepted_request.repository},
        "pull_request": {
            "number": accepted_request.pr_number,
            "draft": False,
            "title": accepted_request.title,
            "body": accepted_request.description,
            "base": {"sha": accepted_request.base_sha},
            "head": {"sha": accepted_request.head_sha},
        },
    }
    caplog.set_level(logging.INFO)

    with _watch_review_failures() as failure_handler, _serve(app) as url:
        status_code, response_body = _send_signed_webhook(
            url,
            payload,
            settings.webhook_secret,
        )
        _wait_for_publication_or_failure(
            publisher,
            failure_handler,
            timeout_seconds=settings.review_timeout_seconds,
            diagnostics=process_runner.diagnostics,
        )

    assert status_code == 202
    assert json.loads(response_body) == {"status": "accepted"}
    assert publisher.body is not None
    assert runner.context is not None
    assert runner.context.request.head_sha == accepted_request.head_sha
    assert runner.context.diff_range.end_sha == accepted_request.head_sha
    assert (
        f"{runner.context.diff_range.start_sha}..{runner.context.diff_range.end_sha}"
        in publisher.body
    )
    assert expected_finding.casefold() in publisher.body.casefold()
    assert all(
        marker not in publisher.body for marker in (forbidden_instruction, forbidden_config)
    )
    _assert_checkpoint_isolation(
        runner=runner,
        sandbox_client=sandbox_client,
        process_runner=process_runner,
        settings=settings,
    )
    observable_text = (
        publisher.body + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    )
    _assert_no_secret_leakage(observable_text, settings)
