import asyncio
import hashlib
import hmac
import json
import logging
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

import pytest
import uvicorn
from fastapi import FastAPI

from review_agent import (
    AgentReview,
    DiffRange,
    FailureCategory,
    ReviewContext,
    Reviewer,
    ReviewError,
    ReviewRequest,
    ReviewResult,
)
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

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del installation_id
        self.comments.append((repository, pr_number, body))
        self.published.set()


class LimitFailingReviewer:
    def __init__(self) -> None:
        self.called = threading.Event()

    def review(self, request: ReviewRequest) -> ReviewResult:
        del request
        self.called.set()
        raise ReviewError(FailureCategory.REVIEW_TOO_LARGE, stage="review_size")


class CleanReviewer:
    def __init__(self) -> None:
        self.reviewed_prs: list[int] = []

    def review(self, request: ReviewRequest) -> ReviewResult:
        self.reviewed_prs.append(request.pr_number)
        return ReviewResult(
            repository=request.repository,
            pr_number=request.pr_number,
            diff_range=DiffRange(start_sha=request.base_sha, end_sha=request.head_sha),
            status="no_important_issues",
            findings=(),
        )


class FirstPublicationFails:
    def __init__(self) -> None:
        self.attempted_prs: list[int] = []
        self.published_prs: list[int] = []
        self.second_published = threading.Event()

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, installation_id, body
        self.attempted_prs.append(pr_number)
        if len(self.attempted_prs) == 1:
            message = "untrusted publication detail"
            raise RuntimeError(message)
        self.published_prs.append(pr_number)
        self.second_published.set()


class FirstRunnerExceedsDeadline:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    def run(self, context: ReviewContext) -> AgentReview:
        del context
        with self._lock:
            self.calls += 1
            call_number = self.calls
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if call_number == 1:
                time.sleep(1.1)
            return AgentReview(findings=())
        finally:
            with self._lock:
                self.active -= 1


class ShutdownCompletingReviewer(CleanReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def review(self, request: ReviewRequest) -> ReviewResult:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError
        return super().review(request)


class FirstReviewIsCancelled(CleanReviewer):
    def review(self, request: ReviewRequest) -> ReviewResult:
        if request.pr_number == 17:
            self.reviewed_prs.append(request.pr_number)
            raise asyncio.CancelledError
        return super().review(request)


class SerialActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0
        self.timeline: list[str] = []

    def start(self, label: str) -> None:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.timeline.append(f"{label}-start")

    def finish(self, label: str) -> None:
        with self._lock:
            self.timeline.append(f"{label}-finish")
            self.active -= 1


class ActivityTrackingReviewer(CleanReviewer):
    def __init__(self, activity: SerialActivity) -> None:
        super().__init__()
        self._activity = activity

    def review(self, request: ReviewRequest) -> ReviewResult:
        label = f"review-{request.pr_number}"
        self._activity.start(label)
        try:
            time.sleep(0.02)
            return super().review(request)
        finally:
            self._activity.finish(label)


class ActivityTrackingPublisher:
    def __init__(self, activity: SerialActivity) -> None:
        self._activity = activity
        self.finished = threading.Event()
        self.published_prs: list[int] = []

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del repository, installation_id, body
        label = f"publish-{pr_number}"
        self._activity.start(label)
        try:
            time.sleep(0.02)
            self.published_prs.append(pr_number)
            if len(self.published_prs) == 2:
                self.finished.set()
        finally:
            self._activity.finish(label)


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
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


def test_review_size_failure_is_logged_and_publishes_no_comment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = LimitFailingReviewer()
    publisher = CapturingPublisher()
    caplog.set_level(logging.WARNING, logger="review_agent.web")
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
            "title": "Too large",
            "body": "untrusted source context",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }

    with _serve(app) as url:
        response = _signed_request(url, payload, "correct horse battery staple")
        assert reviewer.called.wait(timeout=5)
        deadline = time.monotonic() + 1
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.01)

    messages = [record.getMessage() for record in caplog.records]
    assert response == (202, '{"status":"accepted"}')
    assert publisher.comments == []
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'b' * 40} stage=review_size category=review_too_large"
    ]
    assert "untrusted source context" not in messages[0]


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


def test_publication_failure_does_not_stop_later_queued_review(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = CleanReviewer()
    publisher = FirstPublicationFails()
    caplog.set_level(logging.WARNING, logger="review_agent.web")
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    first_payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "First feature",
            "body": "untrusted source context",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }
    second_payload = deepcopy(first_payload)
    second_payload["pull_request"]["number"] = 18  # type: ignore[index]
    second_payload["pull_request"]["head"]["sha"] = "c" * 40  # type: ignore[index]

    with _serve(app) as url:
        responses = [
            _signed_request(url, payload, "correct horse battery staple")
            for payload in (first_payload, second_payload)
        ]
        assert publisher.second_published.wait(timeout=5)

    assert responses == [(202, '{"status":"accepted"}')] * 2
    assert reviewer.reviewed_prs == [17, 18]
    assert publisher.attempted_prs == [17, 18]
    assert publisher.published_prs == [18]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'b' * 40} stage=publication category=review_failure"
    ]
    assert "untrusted publication detail" not in messages[0]


def test_expired_review_deadline_skips_publication_and_allows_later_work(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    runner = FirstRunnerExceedsDeadline()
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=workspace_root,
        runner=runner,
    )
    publisher = CapturingPublisher()
    caplog.set_level(logging.WARNING, logger="review_agent.web")
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
        review_timeout_seconds=1,
    )
    first_payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Slow feature",
            "body": "untrusted source context",
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }
    second_payload = deepcopy(first_payload)
    second_payload["pull_request"]["number"] = 18  # type: ignore[index]

    with _serve(app) as url:
        responses = [
            _signed_request(url, payload, "correct horse battery staple")
            for payload in (first_payload, second_payload)
        ]
        assert publisher.published.wait(timeout=5)

    assert responses == [(202, '{"status":"accepted"}')] * 2
    assert runner.calls == 2
    assert runner.maximum_active == 1
    assert [comment[1] for comment in publisher.comments] == [18]
    assert list(workspace_root.iterdir()) == []
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={head_sha} stage=review_runner category=timeout"
    ]


def test_graceful_shutdown_allows_active_review_to_finish() -> None:
    reviewer = ShutdownCompletingReviewer()
    publisher = CapturingPublisher()
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
        review_timeout_seconds=2,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Feature",
            "body": "Description",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }

    with _serve(app) as url:
        response = _signed_request(url, payload, "correct horse battery staple")
        assert reviewer.started.wait(timeout=5)
        threading.Timer(0.8, reviewer.release.set).start()

    assert response == (202, '{"status":"accepted"}')
    assert reviewer.reviewed_prs == [17]
    assert [comment[1] for comment in publisher.comments] == [17]


def test_cancelled_review_attempt_does_not_stop_later_queued_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewer = FirstReviewIsCancelled()
    publisher = CapturingPublisher()
    caplog.set_level(logging.WARNING, logger="review_agent.web")
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    first_payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Cancelled feature",
            "body": "untrusted source context",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }
    second_payload = deepcopy(first_payload)
    second_payload["pull_request"]["number"] = 18  # type: ignore[index]
    second_payload["pull_request"]["head"]["sha"] = "c" * 40  # type: ignore[index]

    with _serve(app) as url:
        responses = [
            _signed_request(url, payload, "correct horse battery staple")
            for payload in (first_payload, second_payload)
        ]
        assert publisher.published.wait(timeout=5)

    assert responses == [(202, '{"status":"accepted"}')] * 2
    assert reviewer.reviewed_prs == [17, 18]
    assert [comment[1] for comment in publisher.comments] == [18]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "review failed repository=octo-org/example pr_number=17 "
        f"head_sha={'b' * 40} stage=review category=review_failure"
    ]


def test_reviews_and_publications_run_one_at_a_time_in_fifo_order() -> None:
    activity = SerialActivity()
    reviewer = ActivityTrackingReviewer(activity)
    publisher = ActivityTrackingPublisher(activity)
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        reviewer=reviewer,
        publisher=publisher,
    )
    first_payload: dict[str, object] = {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "First feature",
            "body": "Description",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }
    second_payload = deepcopy(first_payload)
    second_payload["pull_request"]["number"] = 18  # type: ignore[index]
    second_payload["pull_request"]["head"]["sha"] = "c" * 40  # type: ignore[index]

    with _serve(app) as url:
        responses = [
            _signed_request(url, payload, "correct horse battery staple")
            for payload in (first_payload, second_payload)
        ]
        assert publisher.finished.wait(timeout=5)

    assert responses == [(202, '{"status":"accepted"}')] * 2
    assert reviewer.reviewed_prs == [17, 18]
    assert publisher.published_prs == [17, 18]
    assert activity.maximum_active == 1
    assert activity.timeline == [
        "review-17-start",
        "review-17-finish",
        "publish-17-start",
        "publish-17-finish",
        "review-18-start",
        "review-18-finish",
        "publish-18-start",
        "publish-18-finish",
    ]
