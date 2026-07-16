import hashlib
import hmac
import json
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError

import uvicorn

from review_agent import AgentReview, ReviewContext, Reviewer
from review_agent.web import create_app


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, filename: str, contents: str, message: str) -> str:
    (repository / filename).write_text(contents, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    base_sha = _commit(repository, "base.txt", "base\n", "base")
    head_sha = _commit(repository, "feature.txt", "feature\n", "feature")
    return repository, base_sha, head_sha


class BlockingRunner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.context: ReviewContext | None = None

    def run(self, context: ReviewContext) -> AgentReview:
        self.context = context
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError
        return AgentReview(findings=())


class CapturingPublisher:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []
        self.published = threading.Event()

    def publish(self, *, repository: str, pr_number: int, body: str) -> None:
        self.comments.append((repository, pr_number, body))
        self.published.set()


@contextmanager
def _serve(app: object) -> Iterator[str]:
    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen()
    host, port = server_socket.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="on"),
    )
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
        message = "test server did not start"
        raise RuntimeError(message)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        server_socket.close()


def _signed_request(
    url: str,
    payload: dict[str, object],
    secret: str,
    *,
    event: str = "pull_request",
) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        f"{url}/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def _raw_request(
    url: str,
    body: bytes,
    *,
    signature: str,
    event: str = "pull_request",
) -> tuple[int, str]:
    request = urllib.request.Request(
        f"{url}/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signed_opened_pull_request_is_accepted_before_review_and_published(
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }

    try:
        with _serve(app) as url:
            status, response_body = _signed_request(
                url,
                payload,
                "correct horse battery staple",
            )

            assert status == 202
            assert json.loads(response_body) == {"status": "accepted"}
            assert runner.started.wait(timeout=5)
            assert runner.context is not None
            assert runner.context.request.repository == "octo-org/example"
            assert runner.context.request.pr_number == 17
            assert runner.context.request.installation_id == 23
            assert runner.context.request.base_sha == base_sha
            assert runner.context.request.head_sha == head_sha
            assert runner.context.request.title == "Add feature"
            assert runner.context.request.description == "A useful description"
            assert publisher.comments == []
            runner.release.set()
            assert publisher.published.wait(timeout=5)
    finally:
        runner.release.set()

    assert len(publisher.comments) == 1
    repository, pr_number, comment = publisher.comments[0]
    assert repository == "octo-org/example"
    assert pr_number == 17
    assert f"{base_sha}..{head_sha}" in comment
    assert "No important issues found" in comment


def test_invalid_signature_is_rejected_before_payload_parsing(tmp_path: Path) -> None:
    source, _, _ = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )

    with _serve(app) as url:
        status, response_body = _raw_request(
            url,
            b"this is not JSON and must not be parsed",
            signature="sha256=" + "0" * 64,
        )

    assert status == 401
    assert json.loads(response_body) == {"detail": "invalid webhook signature"}
    assert not runner.started.is_set()
    assert publisher.comments == []


def test_signed_non_pull_request_event_is_a_successful_no_op(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }

    try:
        with _serve(app) as url:
            status, response_body = _signed_request(
                url,
                payload,
                "correct horse battery staple",
                event="push",
            )
    finally:
        runner.release.set()

    assert status == 200
    assert json.loads(response_body) == {"status": "ignored"}
    assert not runner.started.is_set()
    assert publisher.comments == []


def test_signed_ineligible_pull_requests_are_successful_no_ops(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    eligible: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }
    closed = deepcopy(eligible)
    closed["action"] = "closed"
    draft = deepcopy(eligible)
    draft["pull_request"]["draft"] = True  # type: ignore[index]
    other_repository = deepcopy(eligible)
    other_repository["repository"]["full_name"] = "elsewhere/example"  # type: ignore[index]

    try:
        with _serve(app) as url:
            responses = [
                _signed_request(url, payload, "correct horse battery staple")
                for payload in (closed, draft, other_repository)
            ]
    finally:
        runner.release.set()

    assert responses == [(200, '{"status":"ignored"}')] * 3
    assert not runner.started.is_set()
    assert publisher.comments == []


def test_malformed_eligible_payload_returns_a_generic_client_error(tmp_path: Path) -> None:
    source, _, _ = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    incomplete_payload = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "octo-org/example"},
            "pull_request": {"draft": False},
        }
    ).encode()
    malformed_bodies = (b"not JSON", incomplete_payload)

    with _serve(app) as url:
        responses = [
            _raw_request(
                url,
                body,
                signature=_signature("correct horse battery staple", body),
            )
            for body in malformed_bodies
        ]

    assert responses == [(400, '{"detail":"malformed pull request webhook"}')] * 2
    assert not runner.started.is_set()
    assert publisher.comments == []


def test_full_review_queue_returns_service_unavailable(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }

    try:
        with _serve(app) as url:
            first = _signed_request(url, payload, "correct horse battery staple")
            assert runner.started.wait(timeout=5)
            pending = [
                _signed_request(url, payload, "correct horse battery staple") for _ in range(10)
            ]
            rejected = _signed_request(url, payload, "correct horse battery staple")
            runner.release.set()
            assert publisher.published.wait(timeout=5)
    finally:
        runner.release.set()

    assert first == (202, '{"status":"accepted"}')
    assert pending == [(202, '{"status":"accepted"}')] * 10
    assert rejected == (503, '{"detail":"review queue is full"}')


def test_eligible_webhook_visibly_bounds_the_pull_request_description(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "x" * 10_001,
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }

    try:
        with _serve(app) as url:
            response = _signed_request(url, payload, "correct horse battery staple")
            assert runner.started.wait(timeout=5)
            assert runner.context is not None
            description = runner.context.request.description
            runner.release.set()
            assert publisher.published.wait(timeout=5)
    finally:
        runner.release.set()

    assert response == (202, '{"status":"accepted"}')
    assert len(description) == 10_000
    assert description.endswith("\n\n[truncated]")


def test_duplicate_deliveries_can_create_duplicate_comments(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = BlockingRunner()
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }

    try:
        with _serve(app) as url:
            responses = [
                _signed_request(url, payload, "correct horse battery staple") for _ in range(2)
            ]
            assert runner.started.wait(timeout=5)
            runner.release.set()
            deadline = time.monotonic() + 5
            while len(publisher.comments) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        runner.release.set()

    assert responses == [(202, '{"status":"accepted"}')] * 2
    assert len(publisher.comments) == 2
