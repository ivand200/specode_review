import hashlib
import hmac
import json
from collections.abc import Iterator
from copy import deepcopy
from types import TracebackType
from typing import Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from review_agent.models import ReviewRequest
from review_agent.process_manager import SubmissionOutcome
from review_agent.web import create_app


class ScriptedManager:
    def __init__(
        self,
        outcomes: tuple[SubmissionOutcome, ...] = (SubmissionOutcome.ACCEPTED,),
    ) -> None:
        self._outcomes = iter(outcomes)
        self.submissions: list[ReviewRequest] = []

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
