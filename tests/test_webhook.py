import asyncio
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from copy import deepcopy
from types import TracebackType
from typing import Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from review_agent.attempt import AttemptOutcome
from review_agent.coordinator import RetryReviewRequest, ReviewAttemptCoordinator
from review_agent.github import (
    CHECK_RUN_NAME,
    CheckRun,
    CheckRunConclusion,
    CheckRunStatus,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.models import ReviewRequest
from review_agent.reconciliation import DesiredCheckRun
from review_agent.submission import SubmissionOutcome
from review_agent.web import create_app


class ScriptedManager:
    def __init__(
        self,
        outcomes: tuple[SubmissionOutcome, ...] = (SubmissionOutcome.ACCEPTED,),
    ) -> None:
        self._outcomes = iter(outcomes)
        self.submissions: list[ReviewRequest] = []
        self.retries: list[RetryReviewRequest] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def start(self, request: ReviewRequest) -> SubmissionOutcome:
        self.submissions.append(request)
        return next(self._outcomes)

    async def retry(self, request: RetryReviewRequest) -> SubmissionOutcome:
        self.retries.append(request)
        return next(self._outcomes)


def _manager_app(manager: ScriptedManager) -> FastAPI:
    return create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        manager=manager,
    )


@pytest.fixture
def webhook_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "base": {"sha": "a" * 40},
            "head": {"sha": "b" * 40},
        },
    }


@pytest.fixture
def retry_webhook_payload() -> dict[str, object]:
    return {
        "action": "requested_action",
        "requested_action": {"identifier": "retry_review"},
        "installation": {"id": 23},
        "repository": {"full_name": "octo-org/example"},
        "check_run": {
            "id": 101,
            "name": "Review Agent",
            "head_sha": "b" * 40,
            "external_id": (
                "review-agent:v1:"
                "b3fdc634e74cf30721e4dc24158636348334fa1c133b44a74eb401e89db2119f"
            ),
            "status": "completed",
            "conclusion": "neutral",
            "app": {"id": 12345},
            "output": {
                "title": "Review incomplete — technical failure",
                "summary": "Use Retry review to start a new attempt.",
            },
            "pull_requests": [
                {
                    "number": 17,
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "b" * 40},
                }
            ],
        },
    }


@pytest.fixture
def scripted_manager() -> ScriptedManager:
    return ScriptedManager()


@pytest.fixture
def webhook_client(scripted_manager: ScriptedManager) -> Iterator[TestClient]:
    with TestClient(_manager_app(scripted_manager)) as client:
        yield client


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_raw_webhook(
    client: TestClient,
    body: bytes,
    *,
    signature: str,
    event: str = "pull_request",
) -> Response:
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
        },
    )


def _post_signed_webhook(
    client: TestClient,
    payload: dict[str, object],
    secret: str,
    *,
    event: str = "pull_request",
) -> Response:
    body = json.dumps(payload).encode()
    return _post_raw_webhook(
        client,
        body,
        signature=_signature(secret, body),
        event=event,
    )


def test_signed_opened_pull_request_is_accepted_and_derives_trusted_review_request(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    response = _post_signed_webhook(
        webhook_client,
        webhook_payload,
        "correct horse battery staple",
    )

    assert response.status_code == 202
    assert response.text == '{"status":"accepted"}'
    assert scripted_manager.submissions == [
        ReviewRequest(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            base_sha="a" * 40,
            head_sha="b" * 40,
            title="Add feature",
            description="A useful description",
        )
    ]


def test_signed_retry_action_is_accepted_and_derives_bounded_retry_request(
    webhook_client: TestClient,
    retry_webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    response = _post_signed_webhook(
        webhook_client,
        retry_webhook_payload,
        "correct horse battery staple",
        event="check_run",
    )

    assert response.status_code == 202
    assert response.text == '{"status":"accepted"}'
    assert len(scripted_manager.retries) == 1
    retry = scripted_manager.retries[0]
    assert retry.installation_id == 23
    assert retry.identity.repository == "octo-org/example"
    assert retry.identity.pr_number == 17
    assert retry.identity.base_sha == "a" * 40
    assert retry.identity.head_sha == "b" * 40
    assert retry.check_run.id == 101
    assert scripted_manager.submissions == []


def test_invalid_signature_is_rejected_before_payload_parsing(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    response = _post_raw_webhook(
        webhook_client,
        b"this is not JSON and must not be parsed",
        signature="sha256=" + "0" * 64,
    )

    assert response.status_code == 401
    assert response.text == '{"detail":"invalid webhook signature"}'
    assert scripted_manager.submissions == []


def test_oversized_webhook_is_rejected_despite_an_undersized_content_length(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    body = b"x" * (256 * 1024 + 1)

    response = webhook_client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Length": "1",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _signature("correct horse battery staple", body),
        },
    )

    assert response.status_code == 413
    assert response.text == '{"detail":"webhook body is too large"}'
    assert scripted_manager.submissions == []


def test_oversized_webhook_is_rejected_from_a_chunked_stream_without_content_length(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    body = b"x" * (256 * 1024 + 1)

    response = webhook_client.post(
        "/webhooks/github",
        content=(body[offset : offset + 4096] for offset in range(0, len(body), 4096)),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _signature("correct horse battery staple", body),
        },
    )

    assert response.status_code == 413
    assert response.text == '{"detail":"webhook body is too large"}'
    assert scripted_manager.submissions == []


def test_honest_oversized_content_length_is_rejected_before_reading_or_verification(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    response = webhook_client.post(
        "/webhooks/github",
        content=b"not a signed document",
        headers={
            "Content-Length": str(256 * 1024 + 1),
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "invalid",
        },
    )

    assert response.status_code == 413
    assert response.text == '{"detail":"webhook body is too large"}'
    assert scripted_manager.submissions == []


def test_exact_limit_chunked_body_is_verified_over_all_received_bytes(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    body = b"x" * (256 * 1024)

    response = webhook_client.post(
        "/webhooks/github",
        content=(body[offset : offset + 4096] for offset in range(0, len(body), 4096)),
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _signature("correct horse battery staple", body),
        },
    )

    assert response.status_code == 200
    assert response.text == '{"status":"ignored"}'
    assert scripted_manager.submissions == []


def test_signed_non_pull_request_event_is_a_successful_no_op(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    response = _post_signed_webhook(
        webhook_client,
        webhook_payload,
        "correct horse battery staple",
        event="push",
    )

    assert response.status_code == 200
    assert response.text == '{"status":"ignored"}'
    assert scripted_manager.submissions == []


def test_unrelated_or_untrusted_check_run_actions_are_successful_no_ops(
    webhook_client: TestClient,
    retry_webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    generic_rerequest = deepcopy(retry_webhook_payload)
    generic_rerequest["action"] = "rerequested"
    other_action = deepcopy(retry_webhook_payload)
    other_action["requested_action"]["identifier"] = "other"  # type: ignore[index]
    other_repository = deepcopy(retry_webhook_payload)
    other_repository["repository"]["full_name"] = "elsewhere/example"  # type: ignore[index]
    other_name = deepcopy(retry_webhook_payload)
    other_name["check_run"]["name"] = "Another App"  # type: ignore[index]
    mismatched_identity = deepcopy(retry_webhook_payload)
    mismatched_identity["check_run"]["external_id"] = (  # type: ignore[index]
        "review-agent:v1:" + "0" * 64
    )

    responses = [
        _post_signed_webhook(
            webhook_client,
            payload,
            "correct horse battery staple",
            event="check_run",
        )
        for payload in (
            generic_rerequest,
            other_action,
            other_repository,
            other_name,
            mismatched_identity,
        )
    ]

    assert [(response.status_code, response.text) for response in responses] == [
        (200, '{"status":"ignored"}')
    ] * 5
    assert scripted_manager.retries == []
    assert scripted_manager.submissions == []


def test_signed_ineligible_pull_requests_are_successful_no_ops(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    closed = deepcopy(webhook_payload)
    closed["action"] = "closed"
    draft = deepcopy(webhook_payload)
    draft["pull_request"]["draft"] = True  # type: ignore[index]
    other_repository = deepcopy(webhook_payload)
    other_repository["repository"]["full_name"] = "elsewhere/example"  # type: ignore[index]

    responses = [
        _post_signed_webhook(
            webhook_client,
            payload,
            "correct horse battery staple",
        )
        for payload in (closed, draft, other_repository)
    ]

    assert [(response.status_code, response.text) for response in responses] == [
        (200, '{"status":"ignored"}')
    ] * 3
    assert scripted_manager.submissions == []


def test_malformed_eligible_payload_returns_a_generic_client_error(
    webhook_client: TestClient,
    scripted_manager: ScriptedManager,
) -> None:
    incomplete_payload = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "octo-org/example"},
            "pull_request": {"draft": False},
        }
    ).encode()
    malformed_bodies = (b"not JSON", incomplete_payload)

    responses = [
        _post_raw_webhook(
            webhook_client,
            body,
            signature=_signature("correct horse battery staple", body),
        )
        for body in malformed_bodies
    ]

    assert [(response.status_code, response.text) for response in responses] == [
        (400, '{"detail":"malformed pull request webhook"}')
    ] * 2
    assert scripted_manager.submissions == []


def test_eligible_webhook_visibly_bounds_the_pull_request_description(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    scripted_manager: ScriptedManager,
) -> None:
    webhook_payload["pull_request"]["body"] = "x" * 10_001  # type: ignore[index]

    response = _post_signed_webhook(
        webhook_client,
        webhook_payload,
        "correct horse battery staple",
    )

    assert response.status_code == 202
    assert response.text == '{"status":"accepted"}'
    assert len(scripted_manager.submissions) == 1
    description = scripted_manager.submissions[0].description
    assert len(description) == 10_000
    assert description.endswith("\n\n[truncated]")


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_body"),
    [
        pytest.param(
            SubmissionOutcome.ACCEPTED,
            202,
            '{"status":"accepted"}',
            id="accepted",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_RUNNING,
            200,
            '{"status":"already_running"}',
            id="already-running",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_REVIEWED,
            200,
            '{"status":"already_reviewed"}',
            id="already-reviewed",
        ),
        pytest.param(
            SubmissionOutcome.AT_CAPACITY,
            503,
            '{"detail":"review execution capacity is full"}',
            id="at-capacity",
        ),
        pytest.param(
            SubmissionOutcome.STOPPING,
            503,
            '{"detail":"review service is shutting down"}',
            id="stopping",
        ),
        pytest.param(
            SubmissionOutcome.UNAVAILABLE,
            503,
            '{"detail":"review execution is unavailable"}',
            id="unavailable",
        ),
    ],
)
def test_submission_outcome_maps_to_exact_webhook_response_once(
    webhook_payload: dict[str, object],
    outcome: SubmissionOutcome,
    expected_status: int,
    expected_body: str,
) -> None:
    manager = ScriptedManager((outcome,))

    with TestClient(_manager_app(manager)) as client:
        response = _post_signed_webhook(
            client,
            webhook_payload,
            "correct horse battery staple",
        )

    assert response.status_code == expected_status
    assert response.text == expected_body
    assert len(manager.submissions) == 1


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_body"),
    [
        pytest.param(
            SubmissionOutcome.ACCEPTED,
            202,
            '{"status":"accepted"}',
            id="accepted",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_RUNNING,
            200,
            '{"status":"already_running"}',
            id="duplicate",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_REVIEWED,
            200,
            '{"status":"already_reviewed"}',
            id="stale",
        ),
        pytest.param(
            SubmissionOutcome.AT_CAPACITY,
            503,
            '{"detail":"review execution capacity is full"}',
            id="at-capacity",
        ),
        pytest.param(
            SubmissionOutcome.STOPPING,
            503,
            '{"detail":"review service is shutting down"}',
            id="stopping",
        ),
        pytest.param(
            SubmissionOutcome.UNAVAILABLE,
            503,
            '{"detail":"review execution is unavailable"}',
            id="unavailable",
        ),
    ],
)
def test_retry_outcome_maps_to_the_existing_submission_vocabulary(
    retry_webhook_payload: dict[str, object],
    outcome: SubmissionOutcome,
    expected_status: int,
    expected_body: str,
) -> None:
    manager = ScriptedManager((outcome,))

    with TestClient(_manager_app(manager)) as client:
        response = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )

    assert response.status_code == expected_status
    assert response.text == expected_body
    assert len(manager.retries) == 1
    assert manager.submissions == []


def test_application_factory_coordinates_retry_validation_duplicate_and_replay(  # noqa: C901
    retry_webhook_payload: dict[str, object],
) -> None:
    review_request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Add feature",
        description="A useful description",
    )
    current = CheckRun.model_validate(retry_webhook_payload["check_run"])

    class GitHub:
        def __init__(self) -> None:
            self.current = current
            self.created: CheckRun | None = None

        def list_check_runs(
            self,
            *,
            identity: ReviewIdentity,
            installation_id: int,
        ) -> tuple[CheckRun, ...]:
            assert identity == derive_review_identity(review_request)
            assert installation_id == 23
            return (
                (self.current,)
                if self.created is None
                else (self.current, self.created)
            )

        def create_check_run(
            self,
            *,
            identity: ReviewIdentity,
            installation_id: int,
        ) -> CheckRun:
            assert identity == derive_review_identity(review_request)
            assert installation_id == 23
            self.created = current.model_copy(
                update={
                    "id": 102,
                    "status": CheckRunStatus.QUEUED,
                    "conclusion": None,
                    "output": current.output.model_copy(
                        update={"title": "Review queued"}
                    ),
                }
            )
            return self.created

        def is_owned_check_run(
            self,
            check_run: CheckRun,
            *,
            identity: ReviewIdentity,
        ) -> bool:
            return (
                identity.repository == "octo-org/example"
                and check_run.app.id == 12345
                and check_run.name == CHECK_RUN_NAME
                and check_run.head_sha == identity.head_sha
                and check_run.external_id == identity.external_id
            )

        def get_check_run(self, *, check_run_id: int, installation_id: int) -> CheckRun:
            assert check_run_id == 101
            assert installation_id == 23
            return self.current

        def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
            assert pr_number == 17
            assert installation_id == 23
            return review_request

    class Execution:
        def __init__(self, attempt_id: str, release: threading.Event) -> None:
            self.attempt_id = attempt_id
            self._release = release

        async def wait(self) -> AttemptOutcome:
            await asyncio.to_thread(self._release.wait)
            return AttemptOutcome.model_validate(
                {
                    "attempt_id": self.attempt_id,
                    "status": "reviewed",
                    "review_status": "no_important_issues",
                    "publication": "published",
                    "failure_stage": None,
                    "failure_category": None,
                }
            )

    class Process:
        def __init__(self) -> None:
            self.release = threading.Event()
            self.attempt_ids: list[str] = []
            self.check_run_ids: list[int] = []

        async def launch(
            self,
            request: ReviewRequest,
            *,
            check_run_id: int,
            attempt_id: str | None = None,
        ) -> Execution:
            assert request == review_request
            assert attempt_id is not None
            self.attempt_ids.append(attempt_id)
            self.check_run_ids.append(check_run_id)
            return Execution(attempt_id, self.release)

    class Reconciler:
        def __init__(self, github: GitHub) -> None:
            self.github = github
            self.desired: list[DesiredCheckRun] = []

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def set_desired(self, desired: DesiredCheckRun) -> None:
            self.desired.append(desired)
            target = (
                self.github.created
                if desired.check_run_id == 102
                else self.github.current
            )
            assert target is not None
            if desired.output_kind.value == "running":
                update = {
                    "status": CheckRunStatus.IN_PROGRESS,
                    "conclusion": None,
                    "output": target.output.model_copy(
                        update={"title": "Review in progress"}
                    ),
                }
            else:
                update = {
                    "status": CheckRunStatus.COMPLETED,
                    "conclusion": CheckRunConclusion.SUCCESS,
                    "output": target.output.model_copy(
                        update={"title": "Review complete — no important findings"}
                    ),
                }
            updated = target.model_copy(update=update)
            if desired.check_run_id == 102:
                self.github.created = updated
            else:
                self.github.current = updated

    github = GitHub()
    process = Process()
    reconciler = Reconciler(github)
    coordinator = ReviewAttemptCoordinator(
        github=github,
        process=process,
        reconciler=reconciler,
        installation_id=23,
    )
    app = create_app(
        repository="octo-org/example",
        webhook_secret="correct horse battery staple",
        manager=coordinator,
    )

    with TestClient(app) as client:
        wrong_app = deepcopy(retry_webhook_payload)
        wrong_app["check_run"]["app"]["id"] = 999  # type: ignore[index]
        wrong_app_response = _post_signed_webhook(
            client,
            wrong_app,
            "correct horse battery staple",
            event="check_run",
        )
        assert wrong_app_response.status_code == 200
        assert process.attempt_ids == []

        github.current = current.model_copy(
            update={
                "status": CheckRunStatus.COMPLETED,
                "conclusion": CheckRunConclusion.SUCCESS,
                "output": current.output.model_copy(
                    update={"title": "Review complete — no important findings"}
                ),
            }
        )
        completed_clean = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )
        github.current = current.model_copy(
            update={
                "status": CheckRunStatus.COMPLETED,
                "conclusion": CheckRunConclusion.NEUTRAL,
                "output": current.output.model_copy(
                    update={"title": "Review complete — findings published"}
                ),
            }
        )
        completed_findings = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )
        assert completed_clean.text == '{"status":"already_reviewed"}'
        assert completed_findings.text == '{"status":"already_reviewed"}'
        assert process.attempt_ids == []

        github.current = current
        accepted = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )
        duplicate = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )
        assert accepted.status_code == 202
        assert duplicate.text == '{"status":"already_running"}'
        assert len(process.attempt_ids) == 1
        assert len(process.attempt_ids[0]) == 32
        assert process.check_run_ids == [102]
        assert [state.attempt_id for state in reconciler.desired] == process.attempt_ids

        process.release.set()
        for _ in range(100):
            if len(reconciler.desired) == 2:
                break
            time.sleep(0.01)
        replay = _post_signed_webhook(
            client,
            retry_webhook_payload,
            "correct horse battery staple",
            event="check_run",
        )
        assert replay.text == '{"status":"already_reviewed"}'
        assert len(process.attempt_ids) == 1


def test_liveness_and_readiness_follow_the_application_lifecycle() -> None:
    client = TestClient(_manager_app(ScriptedManager()))

    assert client.get("/health/live").status_code == 200
    before_startup = client.get("/health/ready")
    assert before_startup.status_code == 503
    assert before_startup.text == '{"status":"not_ready"}'

    with client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

        assert live.status_code == 200
        assert live.text == '{"status":"alive"}'
        assert ready.status_code == 200
        assert ready.text == '{"status":"ready"}'

    after_shutdown = client.get("/health/ready")
    assert after_shutdown.status_code == 503
    assert after_shutdown.text == '{"status":"not_ready"}'
