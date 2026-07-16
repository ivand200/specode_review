import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import ReviewRequest, ReviewResult, bound_description
from review_agent.publishing import ReviewPublisher, publish_review_result

logger = logging.getLogger(__name__)
DEFAULT_REVIEW_TIMEOUT_SECONDS = 15 * 60


class ReviewService(Protocol):
    def review(self, request: ReviewRequest) -> ReviewResult: ...


def _run_review_attempt(
    request: ReviewRequest,
    reviewer: ReviewService,
    publisher: ReviewPublisher,
    deadline: ReviewDeadline,
) -> None:
    stage = "review"
    with review_deadline_scope(deadline):
        try:
            deadline.remaining(stage=stage)
            result = reviewer.review(request)
            deadline.remaining(stage=stage)
            stage = "publication"
            deadline.remaining(stage=stage)
            publish_review_result(
                result,
                publisher,
                installation_id=request.installation_id,
            )
        except asyncio.CancelledError:
            logger.warning(
                "review failed repository=%s pr_number=%d head_sha=%s stage=%s category=%s",
                request.repository,
                request.pr_number,
                request.head_sha,
                stage,
                FailureCategory.REVIEW_FAILURE.value,
            )
        except ReviewError as error:
            logger.warning(
                "review failed repository=%s pr_number=%d head_sha=%s stage=%s category=%s",
                request.repository,
                request.pr_number,
                request.head_sha,
                error.stage,
                error.category.value,
            )
        except TimeoutError:
            logger.warning(
                "review failed repository=%s pr_number=%d head_sha=%s stage=%s category=%s",
                request.repository,
                request.pr_number,
                request.head_sha,
                stage,
                FailureCategory.TIMEOUT.value,
            )
        except Exception:  # noqa: BLE001 - this is the worker's failure-isolation boundary.
            logger.warning(
                "review failed repository=%s pr_number=%d head_sha=%s stage=%s category=%s",
                request.repository,
                request.pr_number,
                request.head_sha,
                stage,
                FailureCategory.REVIEW_FAILURE.value,
            )


async def _review_worker(
    queue: asyncio.Queue[ReviewRequest | None],
    reviewer: ReviewService,
    publisher: ReviewPublisher,
    *,
    review_timeout_seconds: float,
    stopping: asyncio.Event,
) -> None:
    while True:
        request = await queue.get()
        if request is None:
            queue.task_done()
            return
        if stopping.is_set():
            queue.task_done()
            return
        deadline = ReviewDeadline.after(review_timeout_seconds)
        try:
            await asyncio.to_thread(
                _run_review_attempt,
                request,
                reviewer,
                publisher,
                deadline,
            )
        finally:
            queue.task_done()
        if stopping.is_set():
            return


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

    try:
        if not request.app.state.accepting_reviews:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="review service is shutting down",
            )
        request.app.state.review_queue.put_nowait(review_request)
    except asyncio.QueueFull as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review queue is full",
        ) from error
    return JSONResponse({"status": "accepted"}, status_code=status.HTTP_202_ACCEPTED)


def create_app(
    *,
    repository: str,
    webhook_secret: str,
    reviewer: ReviewService,
    publisher: ReviewPublisher,
    review_timeout_seconds: float = DEFAULT_REVIEW_TIMEOUT_SECONDS,
) -> FastAPI:
    if review_timeout_seconds <= 0:
        message = "review timeout must be positive"
        raise ValueError(message)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        queue: asyncio.Queue[ReviewRequest | None] = asyncio.Queue(maxsize=10)
        stopping = asyncio.Event()
        app.state.review_queue = queue
        app.state.accepting_reviews = True
        worker = asyncio.create_task(
            _review_worker(
                queue,
                reviewer,
                publisher,
                review_timeout_seconds=review_timeout_seconds,
                stopping=stopping,
            )
        )
        try:
            yield
        finally:
            app.state.accepting_reviews = False
            stopping.set()
            if queue.empty():
                queue.put_nowait(None)
            try:
                await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=review_timeout_seconds,
                )
            except TimeoutError:
                worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(lifespan=lifespan)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> JSONResponse:
        return await _accept_github_webhook(
            request,
            repository=repository,
            webhook_secret=webhook_secret,
        )

    return app
