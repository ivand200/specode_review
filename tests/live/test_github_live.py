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

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from review_agent.attempt import AttemptOutcome
from review_agent.coordinator import ReviewAttemptCoordinator
from review_agent.github import (
    GITHUB_API_VERSION,
    CheckRun,
    CheckRunStatus,
    GitHubAppClient,
    derive_review_identity,
)
from review_agent.live import require_fresh_live_review
from review_agent.models import (
    AcceptedRevision,
    DiffRange,
    Finding,
    Location,
    ReviewRequest,
    ReviewResult,
)
from review_agent.publishing import (
    PublicationDisposition,
    PublicationReceipt,
    publish_review_result,
)
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


def _review_result(
    request: ReviewRequest,
    *,
    findings: tuple[Finding, ...] = (),
) -> ReviewResult:
    return ReviewResult(
        repository=request.repository,
        pr_number=request.pr_number,
        diff_range=DiffRange(
            start_sha=request.base_sha,
            end_sha=request.head_sha,
        ),
        status="issues_found" if findings else "no_important_issues",
        findings=findings,
    )


def _live_finding() -> Finding:
    return Finding(
        severity="important",
        title="Controlled live-profile finding",
        locations=(
            Location(
                path="README.md",
                line=1,
                description="Controlled publication lifecycle evidence.",
            ),
        ),
        evidence="The live profile intentionally changes the complete rendered review body.",
        impact="This proves a successful same-revision publication replaces the prior comment.",
        suggested_fix="Use the final clean result emitted by the controlled profile.",
    )


def _delete_review_comment(
    github: GitHubAppClient,
    request: ReviewRequest,
    *,
    comment_id: int,
) -> None:
    owner, repository_name = request.repository.split("/", maxsplit=1)
    token = github.installation_token(
        repository=request.repository,
        installation_id=request.installation_id,
    )
    response = httpx.delete(
        f"https://api.github.com/repos/{owner}/{repository_name}/issues/comments/{comment_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
        timeout=30,
    )
    assert response.status_code == httpx.codes.NO_CONTENT


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
        self.publication_receipts: tuple[PublicationReceipt, ...] = ()
        self.release = threading.Event()

    async def wait(self) -> AttemptOutcome:
        await asyncio.to_thread(self.release.wait)
        if self._publish_clean_comment:
            clean_result = _review_result(self._request)
            findings_result = _review_result(
                self._request,
                findings=(_live_finding(),),
            )
            created = await asyncio.to_thread(
                publish_review_result,
                request=self._request,
                result=clean_result,
                gateway=self._github,
            )
            updated = await asyncio.to_thread(
                publish_review_result,
                request=self._request,
                result=findings_result,
                gateway=self._github,
            )
            already_current = await asyncio.to_thread(
                publish_review_result,
                request=self._request,
                result=findings_result,
                gateway=self._github,
            )
            await asyncio.to_thread(
                _delete_review_comment,
                self._github,
                self._request,
                comment_id=already_current.comment_id,
            )
            recreated = await asyncio.to_thread(
                publish_review_result,
                request=self._request,
                result=clean_result,
                gateway=self._github,
            )
            self.publication_receipts = (
                created,
                updated,
                already_current,
                recreated,
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
        for check_run in owned:
            active_shape = {
                "Review queued": CheckRunStatus.QUEUED,
                "Review in progress": CheckRunStatus.IN_PROGRESS,
            }.get(check_run.output.title)
            if active_shape is not None and (
                check_run.status is not active_shape or check_run.conclusion is not None
            ):
                pytest.fail(
                    "checkpoint B observed an active Check Run title with a terminal state"
                )
        if callable(predicate):
            matching = tuple(check_run for check_run in owned if predicate(check_run))
            if len(matching) == 1:
                return matching[0]
        time.sleep(0.2)
    pytest.fail("timed out waiting for the expected Review Agent Check Run state")


def _fresh_review_request(
    github: GitHubAppClient,
    *,
    repository: str,
    pr_number: int,
    expected_base_sha: str,
    expected_head_sha: str,
) -> ReviewRequest:
    installation_id = github.repository_installation_id()
    request = github.review_request(pr_number=pr_number, installation_id=installation_id)
    require_fresh_live_review(
        request=request,
        github=github,
        expected=AcceptedRevision(
            repository=repository,
            pr_number=pr_number,
            base_sha=expected_base_sha,
            head_sha=expected_head_sha,
        ),
    )
    return request


def _record_resources(
    path: Path,
    request: ReviewRequest,
    check_run: CheckRun,
    *,
    previous_check_run_id: int,
    comment_id: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as resources:
        resources.write(
            json.dumps(
                {
                    "kind": "github_check_run_and_pull_request_comment",
                    "repository": request.repository,
                    "pr_number": request.pr_number,
                    "base_sha": request.base_sha,
                    "head_sha": request.head_sha,
                    "previous_check_run_id": previous_check_run_id,
                    "check_run_id": check_run.id,
                    "comment_id": comment_id,
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
def test_real_retry_exercises_the_exact_revision_comment_lifecycle(  # noqa: PLR0915
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
    request = _fresh_review_request(
        github,
        repository=repository,
        pr_number=int(_required_environment("E2E_GITHUB_PR_NUMBER")),
        expected_base_sha=_required_environment("E2E_EXPECTED_BASE_SHA"),
        expected_head_sha=_required_environment("E2E_EXPECTED_HEAD_SHA"),
    )
    installation_id = request.installation_id
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
        try:
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
                lambda check: check.status is CheckRunStatus.QUEUED
                and check.conclusion is None
                and check.output.title == "Review queued",
            )
            launcher.allow_launch[0].set()
            assert initial_response.result() == (202, '{"status":"accepted"}')
            running = _wait_for_check_run(
                github,
                request,
                lambda check: check.status is CheckRunStatus.IN_PROGRESS
                and check.conclusion is None
                and check.output.title == "Review in progress",
            )
            assert running.id == queued.id

            launcher.executions[0].release.set()
            retryable = _wait_for_check_run(
                github,
                request,
                lambda check: check.status is CheckRunStatus.COMPLETED
                and check.conclusion == "neutral"
                and check.output.title == "Review incomplete — technical failure",
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
                lambda check: check.status is CheckRunStatus.QUEUED
                and check.conclusion is None
                and check.output.title == "Review queued",
            )
            assert retry_queued.id != queued.id
            launcher.allow_launch[1].set()
            assert retry_response.result() == (202, '{"status":"accepted"}')
            retry_running = _wait_for_check_run(
                github,
                request,
                lambda check: check.status is CheckRunStatus.IN_PROGRESS
                and check.conclusion is None
                and check.output.title == "Review in progress",
            )
            assert retry_running.id == retry_queued.id
            assert launcher.executions[1].attempt_id != launcher.executions[0].attempt_id

            launcher.executions[1].release.set()
            completed = _wait_for_check_run(
                github,
                request,
                lambda check: check.status is CheckRunStatus.COMPLETED
                and check.conclusion == "success"
                and check.output.title == "Review complete — no important findings",
            )
            retained_incomplete = _wait_for_check_run(
                github,
                request,
                lambda check: check.id == retryable.id
                and check.status is CheckRunStatus.COMPLETED
                and check.conclusion == "neutral"
                and check.output.title == "Review incomplete — technical failure",
            )
            receipts = launcher.executions[1].publication_receipts
            marker = f"<!-- {derive_review_identity(request).external_id} -->"
            owned_revision_comments = tuple(
                comment
                for comment in github.list_review_comments(
                    repository=request.repository,
                    pr_number=request.pr_number,
                    installation_id=request.installation_id,
                )
                if comment.body.endswith(f"{marker}\n")
                and comment.performed_via_github_app is not None
                and comment.performed_via_github_app.id == github.app_id
            )
        finally:
            for event in launcher.allow_launch:
                event.set()
            for execution in launcher.executions:
                execution.release.set()
    assert completed.id == retry_queued.id
    assert retained_incomplete.id != completed.id
    assert completed.head_sha == request.head_sha
    assert completed.external_id == derive_review_identity(request).external_id
    assert completed.output.title == "Review complete — no important findings"
    assert [receipt.disposition for receipt in receipts] == [
        PublicationDisposition.CREATED,
        PublicationDisposition.UPDATED,
        PublicationDisposition.ALREADY_CURRENT,
        PublicationDisposition.CREATED,
    ]
    assert receipts[0].comment_id == receipts[1].comment_id == receipts[2].comment_id
    assert receipts[3].comment_id != receipts[2].comment_id
    assert [comment.id for comment in owned_revision_comments] == [receipts[3].comment_id]
    _record_resources(
        resources_path,
        request,
        completed,
        previous_check_run_id=retained_incomplete.id,
        comment_id=receipts[3].comment_id,
    )
