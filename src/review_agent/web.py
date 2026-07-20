import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_agent.models import ReviewRequest, bound_description
from review_agent.submission import ReviewSubmissionLifecycle, SubmissionOutcome

_MAX_WEBHOOK_BODY_BYTES = 256 * 1024
_SUPPORTED_PULL_REQUEST_ACTIONS = frozenset(
    {"opened", "synchronize", "ready_for_review", "reopened"}
)


class _CommitReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")


class _LabelReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = Field(min_length=1, max_length=100)


class _PullRequestPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    number: int = Field(gt=0, strict=True)
    draft: bool
    title: str = Field(min_length=1, max_length=256)
    body: str | None = None
    labels: tuple[_LabelReference, ...] = Field(default=(), max_length=100)
    base: _CommitReference
    head: _CommitReference


class _InstallationReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0, strict=True)


class _RepositoryReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    full_name: str = Field(
        min_length=3,
        max_length=201,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )


class _PullRequestWebhook(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    action: Literal["opened", "synchronize", "ready_for_review", "reopened"]
    installation: _InstallationReference
    repository: _RepositoryReference
    pull_request: _PullRequestPayload


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = 0
        if declared_length > _MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="webhook body is too large",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="webhook body is too large",
            )
        body.extend(chunk)
    return bytes(body)


def _review_request_from_event(event: _PullRequestWebhook) -> ReviewRequest:
    pull_request = event.pull_request
    return ReviewRequest(
        repository=event.repository.full_name.lower(),
        pr_number=pull_request.number,
        installation_id=event.installation.id,
        base_sha=pull_request.base.sha.lower(),
        head_sha=pull_request.head.sha.lower(),
        title=pull_request.title,
        description=bound_description(pull_request.body),
    )


def _payload_action(payload: object) -> str:
    if not isinstance(payload, dict):
        msg = "payload must be an object"
        raise TypeError(msg)
    action = payload.get("action")
    if not isinstance(action, str):
        msg = "payload action must be a string"
        raise TypeError(msg)
    return action


def _event_is_eligible(event: _PullRequestWebhook, no_review_label: str) -> bool:
    if event.pull_request.draft:
        return False
    suppressed_label = no_review_label.casefold()
    return all(
        label.name.casefold() != suppressed_label for label in event.pull_request.labels
    )


def _submission_response(outcome: SubmissionOutcome) -> JSONResponse:
    if outcome is SubmissionOutcome.ALREADY_RUNNING:
        return JSONResponse({"status": "duplicate"})
    if outcome is SubmissionOutcome.ALREADY_REVIEWED:
        return JSONResponse({"status": "ignored"})
    if outcome is SubmissionOutcome.NOT_AUTHORIZED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="repository is not authorized",
        )
    if outcome is SubmissionOutcome.AT_CAPACITY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review execution capacity is full",
        )
    if outcome is SubmissionOutcome.STOPPING:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review service is shutting down",
        )
    if outcome is SubmissionOutcome.UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review execution is unavailable",
        )
    return JSONResponse({"status": "accepted"}, status_code=status.HTTP_202_ACCEPTED)


async def _accept_github_webhook(
    request: Request,
    *,
    webhook_secret: str,
    lifecycle: ReviewSubmissionLifecycle,
    no_review_label: str,
) -> JSONResponse:
    body = await _read_bounded_body(request)
    expected = (
        "sha256="
        + hmac.new(
            webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    supplied = request.headers.get("X-Hub-Signature-256", "")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid webhook signature",
        )
    event_name = request.headers.get("X-GitHub-Event")
    if event_name != "pull_request":
        return JSONResponse({"status": "ignored"})

    try:
        payload = json.loads(body)
        if _payload_action(payload) not in _SUPPORTED_PULL_REQUEST_ACTIONS:
            return JSONResponse({"status": "ignored"})
        event = _PullRequestWebhook.model_validate(payload)
        if not _event_is_eligible(event, no_review_label):
            return JSONResponse({"status": "ignored"})
        review_request = _review_request_from_event(event)
    except (
        AttributeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed pull request webhook",
        ) from error

    return _submission_response(await lifecycle.submit(review_request))


def create_app(
    *,
    webhook_secret: str,
    lifecycle: ReviewSubmissionLifecycle,
    no_review_label: str = "no-review",
    startup_check: Callable[[], None] | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    is_ready = False

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal is_ready
        del app
        if startup_check is not None:
            startup_check()
        try:
            async with lifecycle:
                is_ready = True
                try:
                    yield
                finally:
                    is_ready = False
        finally:
            if shutdown_callback is not None:
                shutdown_callback()

    app = FastAPI(lifespan=lifespan)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        return await _accept_github_webhook(
            request,
            webhook_secret=webhook_secret,
            lifecycle=lifecycle,
            no_review_label=no_review_label,
        )

    @app.get("/health/live")
    async def liveness() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        if is_ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse(
            {"status": "not_ready"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return app
