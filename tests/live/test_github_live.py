import asyncio
import hashlib
import hmac
import json
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from review_agent.attempt import AttemptOutcome
from review_agent.coordinator import ReviewAttemptCoordinator
from review_agent.github import (
    CheckRun,
    CheckRunStatus,
    GitHubAppClient,
    derive_review_identity,
)
from review_agent.models import DiffRange, ReviewRequest, ReviewResult
from review_agent.publishing import render_review_comment
from review_agent.reconciliation import CheckRunReconciler
from review_agent.web import create_app


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
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        message = "live checkpoint server did not start"
        raise RuntimeError(message)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        server_socket.close()


def _send_signed_webhook(
    url: str,
    payload: dict[str, object],
    secret: str,
    *,
    event: str,
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
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode()


class _ControlledExecution:
    def __init__(
        self,
        *,
        attempt_id: str,
        outcome: AttemptOutcome,
        request: ReviewRequest,
        github: GitHubAppClient,
        publish_clean_comment: bool,
    ) -> None:
        self.attempt_id = attempt_id
        self._outcome = outcome
        self._request = request
        self._github = github
        self._publish_clean_comment = publish_clean_comment
        self.release = threading.Event()

    async def wait(self) -> AttemptOutcome:
        await asyncio.to_thread(self.release.wait)
        if self._publish_clean_comment:
            result = ReviewResult(
                repository=self._request.repository,
                pr_number=self._request.pr_number,
                diff_range=DiffRange(
                    start_sha=self._request.base_sha,
                    end_sha=self._request.head_sha,
                ),
                status="no_important_issues",
                findings=(),
            )
            await asyncio.to_thread(
                self._github.publish,
                repository=self._request.repository,
                pr_number=self._request.pr_number,
                installation_id=self._request.installation_id,
                body=render_review_comment(result),
            )
        return self._outcome


class _ControlledLauncher:
    def __init__(self, github: GitHubAppClient) -> None:
        self._github = github
        self.launch_started = (threading.Event(), threading.Event())
        self.allow_launch = (threading.Event(), threading.Event())
        self.executions: list[_ControlledExecution] = []

    async def launch(
        self,
        request: ReviewRequest,
        *,
        check_run_id: int,
        attempt_id: str | None = None,
    ) -> _ControlledExecution:
        del check_run_id
        index = len(self.executions)
        resolved_attempt_id = attempt_id or ("1" * 32)
        if index == 0:
            outcome = AttemptOutcome.model_validate(
                {
                    "attempt_id": resolved_attempt_id,
                    "status": "failed",
                    "review_status": None,
                    "publication": "not_attempted",
                    "failure_stage": "review",
                    "failure_category": "review_failure",
                }
            )
            publish_clean_comment = False
        else:
            outcome = AttemptOutcome.model_validate(
                {
                    "attempt_id": resolved_attempt_id,
                    "status": "reviewed",
                    "review_status": "no_important_issues",
                    "publication": "published",
                    "failure_stage": None,
                    "failure_category": None,
                }
            )
            publish_clean_comment = True
        execution = _ControlledExecution(
            attempt_id=resolved_attempt_id,
            outcome=outcome,
            request=request,
            github=self._github,
            publish_clean_comment=publish_clean_comment,
        )
        self.executions.append(execution)
        self.launch_started[index].set()
        await asyncio.to_thread(self.allow_launch[index].wait)
        return execution


def _pull_request_payload(request: ReviewRequest) -> dict[str, object]:
    return {
        "action": "opened",
        "installation": {"id": request.installation_id},
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


def _retry_payload(request: ReviewRequest, check_run: CheckRun) -> dict[str, object]:
    payload = check_run.model_dump(mode="json")
    payload["pull_requests"] = [
        {
            "number": request.pr_number,
            "base": {"sha": request.base_sha},
            "head": {"sha": request.head_sha},
        }
    ]
    return {
        "action": "requested_action",
        "requested_action": {"identifier": "retry_review"},
        "installation": {"id": request.installation_id},
        "repository": {"full_name": request.repository},
        "check_run": payload,
    }


def _wait_for_check_run(
    github: GitHubAppClient,
    request: ReviewRequest,
    predicate: object,
    *,
    timeout: float = 30,
) -> CheckRun:
    identity = derive_review_identity(request)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        owned = tuple(
            check_run
            for check_run in github.list_check_runs(
                identity=identity,
                installation_id=request.installation_id,
            )
            if github.is_owned_check_run(check_run, identity=identity)
        )
        if len(owned) == 1 and callable(predicate) and predicate(owned[0]):
            return owned[0]
        time.sleep(0.2)
    pytest.fail("timed out waiting for the expected Review Agent Check Run state")


def _record_resources(path: Path, request: ReviewRequest, check_run: CheckRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as resources:
        resources.write(
            json.dumps(
                {
                    "kind": "github_check_run_and_pull_request_comment",
                    "repository": request.repository,
                    "pr_number": request.pr_number,
                    "check_run_id": check_run.id,
                    "cleanup": (
                        "delete the checkpoint B automated review comment; "
                        "the Check Run remains as rollout evidence"
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )


@pytest.mark.live_github
def test_real_check_run_failure_retries_same_check_and_publishes_clean_comment(
    tmp_path: Path,
) -> None:
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
    repository_root = tmp_path / "state"
    repository_root.mkdir(mode=0o700)
    launcher = _ControlledLauncher(github)
    coordinator = ReviewAttemptCoordinator(
        github=github,
        process=launcher,
        reconciler=CheckRunReconciler(
            repository_root=repository_root,
            repository=repository,
            installation_id=installation_id,
            github=github,
        ),
        installation_id=installation_id,
    )
    app = create_app(
        repository=repository,
        webhook_secret=webhook_secret,
        manager=coordinator,
        shutdown_callback=github.close,
    )

    with _serve(app) as url, ThreadPoolExecutor(max_workers=1) as executor:
        initial_response = executor.submit(
            _send_signed_webhook,
            url,
            _pull_request_payload(request),
            webhook_secret,
            event="pull_request",
        )
        assert launcher.launch_started[0].wait(timeout=30)
        queued = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.QUEUED,
        )
        launcher.allow_launch[0].set()
        assert initial_response.result() == (202, '{"status":"accepted"}')
        running = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.IN_PROGRESS,
        )
        assert running.id == queued.id

        launcher.executions[0].release.set()
        retryable = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.COMPLETED
            and check.conclusion == "neutral"
            and [action.identifier for action in check.actions] == ["retry_review"],
        )

        retry_response = executor.submit(
            _send_signed_webhook,
            url,
            _retry_payload(request, retryable),
            webhook_secret,
            event="check_run",
        )
        assert launcher.launch_started[1].wait(timeout=30)
        retry_queued = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.QUEUED,
        )
        assert retry_queued.id == queued.id
        launcher.allow_launch[1].set()
        assert retry_response.result() == (202, '{"status":"accepted"}')
        retry_running = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.IN_PROGRESS,
        )
        assert retry_running.id == queued.id
        assert launcher.executions[1].attempt_id != launcher.executions[0].attempt_id

        launcher.executions[1].release.set()
        completed = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.COMPLETED
            and check.conclusion == "success"
            and not check.actions,
        )

    assert completed.id == queued.id
    assert completed.head_sha == request.head_sha
    assert completed.external_id == derive_review_identity(request).external_id
    assert completed.output.title == "Review complete — no important findings"
    _record_resources(resources_path, request, completed)
