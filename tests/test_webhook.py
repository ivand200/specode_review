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
from pydantic import ValidationError

from specode_review.models import ReviewRequest
from specode_review.submission import SubmissionOutcome
from specode_review.web import create_app


class ScriptedLifecycle:
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

    async def submit(self, request: ReviewRequest) -> SubmissionOutcome:
        self.submissions.append(request)
        return next(self._outcomes)


def _lifecycle_app(
    lifecycle: ScriptedLifecycle,
    *,
    no_review_label: str = "no-review",
) -> FastAPI:
    return create_app(
        webhook_secret="correct horse battery staple",
        lifecycle=lifecycle,
        no_review_label=no_review_label,
    )


@pytest.fixture
def webhook_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "installation": {"id": 23},
        "repository": {"full_name": "Octo-Org/Example"},
        "pull_request": {
            "number": 17,
            "draft": False,
            "title": "Add feature",
            "body": "A useful description",
            "labels": [],
            "base": {"sha": "A" * 40},
            "head": {"sha": "B" * 40},
        },
    }


@pytest.fixture
def lifecycle() -> ScriptedLifecycle:
    return ScriptedLifecycle()


@pytest.fixture
def webhook_client(lifecycle: ScriptedLifecycle) -> Iterator[TestClient]:
    with TestClient(_lifecycle_app(lifecycle)) as client:
        yield client


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_raw_webhook(
    client: TestClient,
    body: bytes,
    *,
    signature: str,
    event: str = "pull_request",
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature,
            **(headers or {}),
        },
    )


def _post_signed_webhook(
    client: TestClient,
    payload: dict[str, object],
    *,
    event: str = "pull_request",
) -> Response:
    body = json.dumps(payload).encode()
    return _post_raw_webhook(
        client,
        body,
        signature=_signature("correct horse battery staple", body),
        event=event,
    )


@pytest.mark.parametrize(
    "action",
    ["opened", "synchronize", "ready_for_review", "reopened"],
)
def test_supported_pull_request_actions_submit_one_normalized_immutable_request(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    lifecycle: ScriptedLifecycle,
    action: str,
) -> None:
    webhook_payload["action"] = action

    response = _post_signed_webhook(webhook_client, webhook_payload)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert lifecycle.submissions == [
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
    lifecycle: ScriptedLifecycle,
) -> None:
    response = _post_raw_webhook(
        webhook_client,
        b"this is not JSON and must not be parsed",
        signature="sha256=" + "0" * 64,
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid webhook signature"}
    assert lifecycle.submissions == []


def test_declared_oversized_body_is_rejected_before_signature_verification(
    webhook_client: TestClient,
    lifecycle: ScriptedLifecycle,
) -> None:
    response = _post_raw_webhook(
        webhook_client,
        b"not signed",
        signature="invalid",
        headers={"Content-Length": str(256 * 1024 + 1)},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "webhook body is too large"}
    assert lifecycle.submissions == []


def test_streamed_oversized_body_is_rejected_even_when_declared_length_is_small(
    webhook_client: TestClient,
    lifecycle: ScriptedLifecycle,
) -> None:
    body = b"x" * (256 * 1024 + 1)

    response = _post_raw_webhook(
        webhook_client,
        body,
        signature=_signature("correct horse battery staple", body),
        headers={"Content-Length": "1"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "webhook body is too large"}
    assert lifecycle.submissions == []


def test_exact_body_limit_is_fully_verified_before_event_policy(
    webhook_client: TestClient,
    lifecycle: ScriptedLifecycle,
) -> None:
    body = b"x" * (256 * 1024)

    response = _post_raw_webhook(
        webhook_client,
        body,
        signature=_signature("correct horse battery staple", body),
        event="push",
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert lifecycle.submissions == []


def test_unsupported_event_and_action_are_ignored_without_submission(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    lifecycle: ScriptedLifecycle,
) -> None:
    malformed_but_signed = b"not JSON"
    unsupported_action = deepcopy(webhook_payload)
    unsupported_action["action"] = "closed"

    responses = [
        _post_raw_webhook(
            webhook_client,
            malformed_but_signed,
            signature=_signature("correct horse battery staple", malformed_but_signed),
            event="push",
        ),
        _post_signed_webhook(webhook_client, unsupported_action),
    ]

    assert [response.json() for response in responses] == [
        {"status": "ignored"},
        {"status": "ignored"},
    ]
    assert lifecycle.submissions == []


def test_draft_and_configured_suppression_label_are_ignored(
    webhook_payload: dict[str, object],
) -> None:
    lifecycle = ScriptedLifecycle()
    draft = deepcopy(webhook_payload)
    draft["pull_request"]["draft"] = True  # type: ignore[index]
    suppressed = deepcopy(webhook_payload)
    suppressed["pull_request"]["labels"] = [  # type: ignore[index]
        {"name": "keep"},
        {"name": "SKIP-REVIEW"},
    ]

    with TestClient(
        _lifecycle_app(lifecycle, no_review_label="skip-review")
    ) as client:
        responses = [
            _post_signed_webhook(client, draft),
            _post_signed_webhook(client, suppressed),
        ]

    assert [response.json() for response in responses] == [
        {"status": "ignored"},
        {"status": "ignored"},
    ]
    assert lifecycle.submissions == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not JSON", id="invalid-json"),
        pytest.param(
            json.dumps(
                {
                    "action": "opened",
                    "repository": {"full_name": "octo-org/example"},
                    "pull_request": {"draft": False},
                }
            ).encode(),
            id="missing-fields",
        ),
        pytest.param(
            json.dumps(
                {
                    "action": "opened",
                    "installation": {"id": 23},
                    "repository": {"full_name": "octo-org/example"},
                    "pull_request": {
                        "number": 17,
                        "draft": False,
                        "title": "Review",
                        "labels": "no-review",
                        "base": {"sha": "a" * 40},
                        "head": {"sha": "b" * 40},
                    },
                }
            ).encode(),
            id="malformed-labels",
        ),
    ],
)
def test_malformed_supported_event_returns_generic_client_error(
    webhook_client: TestClient,
    lifecycle: ScriptedLifecycle,
    payload: bytes,
) -> None:
    response = _post_raw_webhook(
        webhook_client,
        payload,
        signature=_signature("correct horse battery staple", payload),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "malformed pull request webhook"}
    assert lifecycle.submissions == []


def test_review_context_is_bounded_and_unrelated_payload_fields_are_discarded(
    webhook_client: TestClient,
    webhook_payload: dict[str, object],
    lifecycle: ScriptedLifecycle,
) -> None:
    webhook_payload["sender"] = {"login": "untrusted"}
    webhook_payload["pull_request"]["body"] = "x" * 10_001  # type: ignore[index]
    webhook_payload["pull_request"]["user"] = {"login": "untrusted"}  # type: ignore[index]

    response = _post_signed_webhook(webhook_client, webhook_payload)

    assert response.status_code == 202
    request = lifecycle.submissions[0]
    assert len(request.description) == 10_000
    assert request.description.endswith("\n\n[truncated]")
    assert set(request.model_dump()) == {
        "repository",
        "pr_number",
        "installation_id",
        "base_sha",
        "head_sha",
        "title",
        "description",
    }
    with pytest.raises(ValidationError, match="frozen"):
        request.title = "changed"


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_body"),
    [
        pytest.param(
            SubmissionOutcome.ACCEPTED,
            202,
            {"status": "accepted"},
            id="accepted",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_RUNNING,
            200,
            {"status": "duplicate"},
            id="duplicate",
        ),
        pytest.param(
            SubmissionOutcome.ALREADY_REVIEWED,
            200,
            {"status": "ignored"},
            id="already-reviewed",
        ),
        pytest.param(
            SubmissionOutcome.NOT_AUTHORIZED,
            403,
            {"detail": "repository is not authorized"},
            id="not-authorized",
        ),
        pytest.param(
            SubmissionOutcome.STOPPING,
            503,
            {"detail": "review service is shutting down"},
            id="stopping",
        ),
        pytest.param(
            SubmissionOutcome.AT_CAPACITY,
            503,
            {"detail": "review execution capacity is full"},
            id="capacity",
        ),
        pytest.param(
            SubmissionOutcome.UNAVAILABLE,
            503,
            {"detail": "review execution is unavailable"},
            id="unavailable",
        ),
    ],
)
def test_lifecycle_outcome_maps_to_one_http_response(
    webhook_payload: dict[str, object],
    outcome: SubmissionOutcome,
    expected_status: int,
    expected_body: dict[str, str],
) -> None:
    lifecycle = ScriptedLifecycle((outcome,))

    with TestClient(_lifecycle_app(lifecycle)) as client:
        response = _post_signed_webhook(client, webhook_payload)

    assert response.status_code == expected_status
    assert response.json() == expected_body
    assert len(lifecycle.submissions) == 1


def test_liveness_and_readiness_follow_lifecycle_entry_and_exit() -> None:
    client = TestClient(_lifecycle_app(ScriptedLifecycle()))

    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").status_code == 503

    with client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").json() == {"status": "ready"}

    assert client.get("/health/ready").status_code == 503
