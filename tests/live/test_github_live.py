import hashlib
import hmac
import json
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from review_agent import (
    CandidateAcceptance,
    GitHubRepository,
    ReviewContext,
    Reviewer,
)
from review_agent.configuration import DEFAULT_REVIEW_TIMEOUT_SECONDS
from review_agent.core import CandidateContract
from review_agent.github import GitHubAppClient
from review_agent.publishing import ReviewPublisher
from review_agent.web import create_app
from review_agent.worker import SingleReviewWorker


class CleanAdapter:
    def __init__(self) -> None:
        self.context: ReviewContext | None = None

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.context = context
        return b'{"findings":[]}'


class RecordingPublisher:
    def __init__(
        self,
        publisher: ReviewPublisher,
        *,
        resources_path: Path,
    ) -> None:
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
                        "cleanup": "delete the automated review comment created by checkpoint B",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        self.published.set()


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for the live GitHub profile")
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
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        message = "live checkpoint server did not start"
        raise RuntimeError(message)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
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
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode()


@pytest.mark.live_github
def test_signed_webhook_reviews_and_comments_on_real_github_pr(tmp_path: Path) -> None:
    if os.environ.get("RUN_LIVE_GITHUB_E2E") != "1":
        pytest.skip("set RUN_LIVE_GITHUB_E2E=1 to enable the live GitHub profile")

    repository = _required_environment("E2E_GITHUB_REPOSITORY")
    if repository != _required_environment("GITHUB_REPOSITORY"):
        pytest.fail("E2E_GITHUB_REPOSITORY must equal the configured repository")
    if "test" not in repository.casefold():
        pytest.fail("the live GitHub profile requires an explicitly named test repository")

    webhook_secret = _required_environment("GITHUB_WEBHOOK_SECRET")
    resources_path = Path(_required_environment("E2E_CREATED_RESOURCES_PATH"))
    github = GitHubAppClient(
        repository=repository,
        app_id=int(_required_environment("GITHUB_APP_ID")),
        private_key_path=Path(_required_environment("GITHUB_PRIVATE_KEY_PATH")),
    )
    installation_id = github.repository_installation_id()
    request = github.review_request(
        pr_number=int(_required_environment("E2E_GITHUB_PR_NUMBER")),
        installation_id=installation_id,
    )
    runner = CleanAdapter()
    reviewer = Reviewer(
        repository=repository,
        workspace_root=tmp_path / "workspaces",
        candidate_acceptance=CandidateAcceptance(adapter=runner, max_bytes=65_536),
        source_repository=GitHubRepository(credentials=github),
    )
    publisher = RecordingPublisher(github, resources_path=resources_path)
    app = create_app(
        repository=repository,
        webhook_secret=webhook_secret,
        worker=SingleReviewWorker(
            reviewer=reviewer,
            publisher=publisher,
            review_timeout_seconds=DEFAULT_REVIEW_TIMEOUT_SECONDS,
        ),
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": installation_id},
        "repository": {"full_name": request.repository},
        "pull_request": {
            "number": request.pr_number,
            "draft": False,
            "title": request.title,
            "body": request.description,
            "base": {"sha": request.base_sha},
            "head": {"sha": request.head_sha},
        },
    }

    try:
        with _serve(app) as url:
            status_code, response_body = _send_signed_webhook(url, payload, webhook_secret)
            assert publisher.published.wait(timeout=120)
    finally:
        github.close()

    assert status_code == 202
    assert json.loads(response_body) == {"status": "accepted"}
    assert runner.context is not None
    assert runner.context.request.head_sha == request.head_sha
    assert runner.context.diff_range.end_sha == request.head_sha
    assert publisher.body is not None
    assert f"{runner.context.diff_range.start_sha}..{request.head_sha}" in publisher.body
    assert "No important issues found" in publisher.body
