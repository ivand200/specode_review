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

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from review_agent.configuration import ProductionSettings
from review_agent.github import CheckRun, CheckRunStatus, GitHubAppClient, derive_review_identity
from review_agent.live import require_fresh_live_review
from review_agent.models import ReviewRequest
from review_agent.production import create_production_app
from review_agent.sandbox import DockerSandboxClient


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
    deadline = time.monotonic() + 60
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        message = "checkpoint C production server did not start"
        raise RuntimeError(message)
    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=60)
        server_socket.close()
        if thread.is_alive():
            pytest.fail("checkpoint C production shutdown timed out")


def _send_signed_webhook(
    url: str,
    payload: dict[str, object],
    secret: str,
) -> tuple[int, str]:
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


def _wait_for_check_run(
    github: GitHubAppClient,
    request: ReviewRequest,
    predicate: object,
    *,
    timeout: float,
) -> CheckRun:
    identity = derive_review_identity(request)
    deadline = time.monotonic() + timeout
    last: CheckRun | None = None
    while time.monotonic() < deadline:
        owned = tuple(
            check_run
            for check_run in github.list_check_runs(
                identity=identity,
                installation_id=request.installation_id,
            )
            if github.is_owned_check_run(check_run, identity=identity)
        )
        if len(owned) == 1:
            last = owned[0]
            if callable(predicate) and predicate(last):
                return last
        time.sleep(0.5)
    observed = "none" if last is None else f"{last.status}/{last.conclusion}"
    pytest.fail(f"checkpoint C timed out waiting for Check Run state; last={observed}")


def _find_published_comment(
    *,
    repository: str,
    pr_number: int,
    installation_token: str,
    accepted_head: str,
    expected_finding: str,
) -> tuple[int, str]:
    owner, name = repository.split("/", maxsplit=1)
    response = httpx.get(
        f"https://api.github.com/repos/{owner}/{name}/issues/{pr_number}/comments",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {installation_token}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        params={"per_page": 100, "sort": "created", "direction": "desc"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        pytest.fail("checkpoint C received a malformed comment list")
    for item in payload:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("id")
        body = item.get("body")
        if (
            isinstance(comment_id, int)
            and isinstance(body, str)
            and body.startswith("# Automated code review")
            and accepted_head in body
            and expected_finding.casefold() in body.casefold()
        ):
            return comment_id, body
    pytest.fail("checkpoint C could not find the validated automated review comment")


def _record_resources(
    path: Path,
    *,
    repository: str,
    pr_number: int,
    check_run_id: int,
    comment_id: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as resources:
        resources.write(
            json.dumps(
                {
                    "kind": "full_live_github_resources",
                    "repository": repository,
                    "pr_number": pr_number,
                    "check_run_id": check_run_id,
                    "comment_id": comment_id,
                    "cleanup": (
                        "delete the recorded pull-request comment; "
                        "retain the Check Run as rollout evidence"
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )


@pytest.mark.live_full
def test_full_live_production_lifecycle_reviews_and_publishes() -> None:
    if os.environ.get("RUN_FULL_LIVE_E2E") != "1":
        pytest.skip("set RUN_FULL_LIVE_E2E=1 to enable checkpoint C")
    if os.environ.get("ACKNOWLEDGE_MODEL_COST") != "1":
        pytest.fail("set ACKNOWLEDGE_MODEL_COST=1 to approve the checkpoint C model call")

    settings = ProductionSettings.from_environment(os.environ)
    webhook = settings.webhook
    attempt = settings.attempt
    repository = _required_environment("E2E_GITHUB_REPOSITORY")
    if repository != webhook.repository or "test" not in repository.casefold():
        pytest.fail("checkpoint C requires the configured dedicated test repository")
    expected_finding = _required_environment("E2E_EXPECTED_FINDING")
    forbidden_instruction = _required_environment("E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT")
    forbidden_config = _required_environment("E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT")
    resources_path = Path(_required_environment("E2E_CREATED_RESOURCES_PATH"))
    if attempt.workspace_root.resolve().is_relative_to(Path.cwd().resolve()):
        pytest.fail("checkpoint C workspace must not be inside the project working copy")
    if attempt.workspace_root.exists() and any(attempt.workspace_root.iterdir()):
        pytest.fail("checkpoint C requires an empty dedicated workspace root")

    github = GitHubAppClient(
        repository=repository,
        app_id=attempt.app_id,
        private_key_path=attempt.private_key_path,
    )
    installation_id = github.repository_installation_id()
    request = github.review_request(
        pr_number=int(_required_environment("E2E_GITHUB_PR_NUMBER")),
        installation_id=installation_id,
    )
    require_fresh_live_review(request=request, github=github)
    installation_token = github.installation_token(
        repository=repository,
        installation_id=installation_id,
    )
    sandbox_client = DockerSandboxClient(config=attempt.runtime.sandbox_operation)
    app = create_production_app(
        settings=settings,
        environment=os.environ,
        github_client=github,
        sandbox_client=sandbox_client,
    )

    with _serve(app) as url:
        status_code, response_body = _send_signed_webhook(
            url,
            _pull_request_payload(request),
            webhook.secret,
        )
        assert status_code == 202
        assert json.loads(response_body) == {"status": "accepted"}
        running = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.IN_PROGRESS,
            timeout=30,
        )
        completed = _wait_for_check_run(
            github,
            request,
            lambda check: check.status is CheckRunStatus.COMPLETED,
            timeout=attempt.runtime.review_timeout_seconds
            + attempt.runtime.sandbox_operation.cleanup_timeout_seconds
            + 60,
        )
        comment_id, comment_body = _find_published_comment(
            repository=repository,
            pr_number=request.pr_number,
            installation_token=installation_token,
            accepted_head=request.head_sha,
            expected_finding=expected_finding,
        )

    assert running.id == completed.id
    assert completed.head_sha == request.head_sha
    assert completed.external_id == derive_review_identity(request).external_id
    assert completed.conclusion == "neutral"
    assert expected_finding.casefold() in comment_body.casefold()
    assert forbidden_instruction not in comment_body
    assert forbidden_config not in comment_body
    assert list(attempt.workspace_root.iterdir()) == []
    assert not any(
        name.startswith(attempt.runtime.sandbox_name_prefix) for name in sandbox_client.list_names()
    )
    _record_resources(
        resources_path,
        repository=repository,
        pr_number=request.pr_number,
        check_run_id=completed.id,
        comment_id=comment_id,
    )
