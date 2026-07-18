import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from review_agent.models import ReviewRequest, bound_description
from review_agent.process_manager import ReviewExecutionManager, SubmissionOutcome


def _review_request_from_payload(payload: object) -> ReviewRequest:
    if not isinstance(payload, dict):
        msg = "payload must be an object"
        raise TypeError(msg)
    pull_request = payload["pull_request"]
    installation = payload["installation"]
    repository = payload["repository"]
    return ReviewRequest(
        repository=repository["full_name"],
        pr_number=pull_request["number"],
        installation_id=installation["id"],
        base_sha=pull_request["base"]["sha"],
        head_sha=pull_request["head"]["sha"],
        title=pull_request["title"],
        description=bound_description(pull_request.get("body")),
    )


def _payload_is_eligible(payload: object, repository: str) -> bool:
    if not isinstance(payload, dict):
        msg = "payload must be an object"
        raise TypeError(msg)

    action = payload.get("action")
    if not isinstance(action, str):
        msg = "payload action must be a string"
        raise TypeError(msg)
    if action != "opened":
        return False

    repository_payload = payload.get("repository")
    if not isinstance(repository_payload, dict):
        msg = "payload repository must be an object"
        raise TypeError(msg)
    full_name = repository_payload.get("full_name")
    if not isinstance(full_name, str):
        msg = "payload repository name must be a string"
        raise TypeError(msg)
    if full_name != repository:
        return False

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        msg = "payload pull request must be an object"
        raise TypeError(msg)
    draft = pull_request.get("draft")
    if not isinstance(draft, bool):
        msg = "pull request draft state must be a boolean"
        raise TypeError(msg)
    return not draft


async def _accept_github_webhook(
    request: Request,
    *,
    repository: str,
    webhook_secret: str,
    manager: ReviewExecutionManager,
) -> JSONResponse:
    body = await request.body()
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
    if request.headers.get("X-GitHub-Event") != "pull_request":
        return JSONResponse({"status": "ignored"})

    try:
        payload = json.loads(body)
        if not _payload_is_eligible(payload, repository):
            return JSONResponse({"status": "ignored"})
        review_request = _review_request_from_payload(payload)
    except (
        AttributeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed pull request webhook",
        ) from error

    outcome = await manager.start(review_request)
    if outcome is SubmissionOutcome.ALREADY_RUNNING:
        return JSONResponse({"status": "already_running"})
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


def create_app(
    *,
    repository: str,
    webhook_secret: str,
    manager: ReviewExecutionManager,
    startup_check: Callable[[], None] | None = None,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        if startup_check is not None:
            startup_check()
        try:
            async with manager:
                yield
        finally:
            if shutdown_callback is not None:
                shutdown_callback()

    app = FastAPI(lifespan=lifespan)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        return await _accept_github_webhook(
            request,
            repository=repository,
            webhook_secret=webhook_secret,
            manager=manager,
        )

    return app
