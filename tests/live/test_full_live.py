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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.core import GitHubRepository, ReviewContext, Reviewer, ReviewLimits
from review_agent.github import GitHubAppClient
from review_agent.publishing import ReviewPublisher
from review_agent.readiness import ProductionReadiness
from review_agent.sandbox import CodexSandboxRunner, DockerSandboxClient, DockerSandboxConfig
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
    def __init__(self, runner: CodexSandboxRunner) -> None:
        self._runner = runner
        self.context: ReviewContext | None = None
        self.host_checkout_unchanged = False

    def run(self, context: ReviewContext) -> object:
        self.context = context
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
    sandbox_client = DockerSandboxClient(
        config=DockerSandboxConfig(
            process_output_max_bytes=settings.process_output_max_bytes,
            cleanup_timeout_seconds=settings.sandbox_cleanup_timeout_seconds,
        )
    )
    runner = RecordingRunner(
        CodexSandboxRunner(
            client=sandbox_client,
            sandbox_prefix=settings.sandbox_name_prefix,
            kit=settings.review_kit_path,
            model=settings.codex_model,
            candidate_output_max_bytes=settings.candidate_output_max_bytes,
        )
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

    with _serve(app) as url:
        status_code, response_body = _send_signed_webhook(
            url,
            payload,
            settings.webhook_secret,
        )
        assert publisher.published.wait(timeout=settings.review_timeout_seconds)

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
    assert forbidden_instruction not in publisher.body
    assert runner.host_checkout_unchanged
    assert list(settings.workspace_root.iterdir()) == []
    assert not any(
        name.startswith(settings.sandbox_name_prefix) for name in sandbox_client.list_names()
    )
    observable_text = (
        publisher.body + "\n" + "\n".join(record.getMessage() for record in caplog.records)
    )
    assert settings.webhook_secret not in observable_text
    assert "OPENAI_API_KEY" not in observable_text
