import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
)

from specode_review.deadline import remaining_review_time
from specode_review.errors import FailureCategory, ReviewError
from specode_review.models import ReviewRequest, Sha, bound_description

GITHUB_API_VERSION = "2026-03-10"
REVIEW_COMMENT_PAGE_SIZE = 100
REVIEW_COMMENT_MAX_PAGES = 10
GITHUB_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
class GitHubOperation(StrEnum):
    INSTALLATION_READ = "installation_read"
    INSTALLATION_TOKEN = "installation_token"  # noqa: S105 - normalized operation name.
    PULL_REQUEST_READ = "pull_request_read"
    REVIEW_COMMENT_CREATE = "review_comment_create"
    REVIEW_COMMENT_LIST = "review_comment_list"
    REVIEW_COMMENT_UPDATE = "review_comment_update"
    WEBHOOK_CONFIGURATION_READ = "webhook_configuration_read"


class GitHubError(Exception):
    def __init__(
        self,
        operation: GitHubOperation,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        status_suffix = f" with status {status_code}" if status_code is not None else ""
        super().__init__(f"GitHub {operation.value} failed{status_suffix}")


class GitHubMutationError(GitHubError):
    """A comment mutation failure whose remote result may be ambiguous."""


@dataclass(frozen=True, slots=True)
class _Request:
    operation: GitHubOperation
    method: Literal["GET", "PATCH", "POST"]
    path: str
    bearer: str
    expected_status: int
    json_body: dict[str, Any] | None = None


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str = Field(min_length=1)


class _InstallationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)


class _WebhookConfigurationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1)


class _CommitIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: Sha


class _PullRequestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int = Field(gt=0)
    title: str
    body: str | None
    base: _CommitIdentity
    head: _CommitIdentity


class ReviewCommentApp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0, strict=True)


class ReviewComment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0, strict=True)
    body: str = Field(strict=True)
    performed_via_github_app: ReviewCommentApp | None


class _ReviewCommentPage(RootModel[tuple[ReviewComment, ...]]):
    root: tuple[ReviewComment, ...] = Field(max_length=REVIEW_COMMENT_PAGE_SIZE)


class ReviewCommentGateway(Protocol):
    @property
    def app_id(self) -> int: ...

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]: ...

    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment: ...

    def update_review_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment: ...


class GitHubAppClient:
    def __init__(
        self,
        *,
        repository: str,
        app_id: int,
        private_key_path: Path,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._app_id = app_id
        self._private_key_path = private_key_path
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(base_url="https://api.github.com")
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    @property
    def app_id(self) -> int:
        return self._app_id

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def _app_jwt(self, operation: GitHubOperation) -> str:
        now = int(self._clock().timestamp())
        try:
            return jwt.encode(
                {
                    "iat": now - 60,
                    "exp": now + 600,
                    "iss": str(self._app_id),
                },
                self._private_key_path.read_bytes(),
                algorithm="RS256",
            )
        except (OSError, ValueError, jwt.PyJWTError):
            raise GitHubError(operation) from None

    def _request(self, request: _Request) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {request.bearer}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        timeout = remaining_review_time(stage=request.operation.value)
        request_options: dict[str, Any] = {}
        if timeout is not None:
            request_options["timeout"] = timeout
        try:
            if request.json_body is None:
                response = self._http.request(
                    request.method,
                    request.path,
                    headers=headers,
                    **request_options,
                )
            else:
                response = self._http.request(
                    request.method,
                    request.path,
                    headers=headers,
                    json=request.json_body,
                    **request_options,
                )
        except httpx.TimeoutException:
            if self._is_review_comment_mutation(request.operation):
                raise GitHubMutationError(request.operation) from None
            raise ReviewError(
                FailureCategory.TIMEOUT,
                stage=request.operation.value,
            ) from None
        except httpx.HTTPError:
            if self._is_review_comment_mutation(request.operation):
                raise GitHubMutationError(request.operation) from None
            raise GitHubError(request.operation) from None
        if response.status_code != request.expected_status:
            retry_after_seconds = self._retry_after_seconds(response)
            error_type = (
                GitHubMutationError
                if self._is_review_comment_mutation(request.operation)
                and (
                    response.status_code in {408, 429, 500, 502, 503, 504}
                    or retry_after_seconds is not None
                )
                else GitHubError
            )
            raise error_type(
                request.operation,
                status_code=response.status_code,
                retry_after_seconds=retry_after_seconds,
            ) from None
        return response

    @staticmethod
    def _is_review_comment_mutation(operation: GitHubOperation) -> bool:
        return operation in {
            GitHubOperation.REVIEW_COMMENT_CREATE,
            GitHubOperation.REVIEW_COMMENT_UPDATE,
        }

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                except (TypeError, ValueError):
                    retry_at = None
                if retry_at is not None:
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    seconds = (retry_at - self._clock()).total_seconds()
                else:
                    seconds = -1
            if math.isfinite(seconds) and seconds >= 0:
                return seconds

        if response.headers.get("X-RateLimit-Remaining") != "0":
            return None
        reset = response.headers.get("X-RateLimit-Reset")
        if reset is None:
            return None
        try:
            reset_at = float(reset)
        except ValueError:
            return None
        seconds = max(0.0, reset_at - self._clock().timestamp())
        return seconds if math.isfinite(seconds) else None

    @staticmethod
    def _validate_response(
        model: type[BaseModel],
        response: httpx.Response,
        operation: GitHubOperation,
    ) -> BaseModel:
        if len(response.content) > GITHUB_RESPONSE_MAX_BYTES:
            raise GitHubError(operation)
        try:
            return model.model_validate(response.json())
        except (ValidationError, ValueError):
            raise GitHubError(operation) from None

    def installation_token(self, *, repository: str, installation_id: int) -> str:
        if repository != self._repository:
            message = "repository does not match the configured GitHub repository"
            raise ValueError(message)

        operation = GitHubOperation.INSTALLATION_TOKEN
        app_jwt = self._app_jwt(operation)
        repository_name = repository.partition("/")[2]
        response = self._request(
            _Request(
                operation=operation,
                method="POST",
                path=f"/app/installations/{installation_id}/access_tokens",
                bearer=app_jwt,
                expected_status=httpx.codes.CREATED,
                json_body={
                    "repositories": [repository_name],
                    "permissions": {
                        "contents": "read",
                        "pull_requests": "write",
                    },
                },
            )
        )
        payload = self._validate_response(_TokenResponse, response, operation)
        if not isinstance(payload, _TokenResponse):
            raise GitHubError(operation)
        return payload.token

    def repository_installation_id(self) -> int:
        operation = GitHubOperation.INSTALLATION_READ
        app_jwt = self._app_jwt(operation)
        owner, repository_name = self._repository.split("/", maxsplit=1)
        response = self._request(
            _Request(
                operation=operation,
                method="GET",
                path=f"/repos/{owner}/{repository_name}/installation",
                bearer=app_jwt,
                expected_status=httpx.codes.OK,
            )
        )
        payload = self._validate_response(_InstallationResponse, response, operation)
        if not isinstance(payload, _InstallationResponse):
            raise GitHubError(operation)
        return payload.id

    def webhook_url(self) -> str:
        operation = GitHubOperation.WEBHOOK_CONFIGURATION_READ
        app_jwt = self._app_jwt(operation)
        response = self._request(
            _Request(
                operation=operation,
                method="GET",
                path="/app/hook/config",
                bearer=app_jwt,
                expected_status=httpx.codes.OK,
            )
        )
        payload = self._validate_response(
            _WebhookConfigurationResponse,
            response,
            operation,
        )
        if not isinstance(payload, _WebhookConfigurationResponse):
            raise GitHubError(operation)
        return payload.url

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        if repository != self._repository:
            message = "repository does not match the configured GitHub repository"
            raise ValueError(message)
        if pr_number < 1:
            message = "pr_number must be positive"
            raise ValueError(message)
        token = self.installation_token(
            repository=repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.REVIEW_COMMENT_LIST
        owner, repository_name = repository.split("/", maxsplit=1)
        comments: list[ReviewComment] = []
        next_path: str | None = (
            f"/repos/{owner}/{repository_name}/issues/{pr_number}/comments?"
            f"{urlencode({'per_page': str(REVIEW_COMMENT_PAGE_SIZE)})}"
        )
        for _ in range(REVIEW_COMMENT_MAX_PAGES):
            if next_path is None:
                return tuple(comments)
            response = self._request(
                _Request(
                    operation=operation,
                    method="GET",
                    path=next_path,
                    bearer=token,
                    expected_status=httpx.codes.OK,
                )
            )
            payload = self._validate_response(_ReviewCommentPage, response, operation)
            if not isinstance(payload, _ReviewCommentPage):
                raise GitHubError(operation)
            comments.extend(payload.root)
            next_path = self._review_comment_next_path(
                response,
                expected_path=f"/repos/{owner}/{repository_name}/issues/{pr_number}/comments",
                operation=operation,
            )
        if next_path is not None:
            raise GitHubError(operation)
        return tuple(comments)

    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        if repository != self._repository:
            message = "repository does not match the configured GitHub repository"
            raise ValueError(message)
        if pr_number < 1:
            message = "pr_number must be positive"
            raise ValueError(message)
        if not body:
            message = "comment body must not be empty"
            raise ValueError(message)
        token = self.installation_token(
            repository=repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.REVIEW_COMMENT_CREATE
        owner, repository_name = repository.split("/", maxsplit=1)
        response = self._request(
            _Request(
                operation=operation,
                method="POST",
                path=f"/repos/{owner}/{repository_name}/issues/{pr_number}/comments",
                bearer=token,
                expected_status=httpx.codes.CREATED,
                json_body={"body": body},
            )
        )
        return self._review_comment_response(response, operation)

    def update_review_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        if repository != self._repository:
            message = "repository does not match the configured GitHub repository"
            raise ValueError(message)
        if comment_id < 1:
            message = "comment_id must be positive"
            raise ValueError(message)
        if not body:
            message = "comment body must not be empty"
            raise ValueError(message)
        token = self.installation_token(
            repository=repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.REVIEW_COMMENT_UPDATE
        owner, repository_name = repository.split("/", maxsplit=1)
        response = self._request(
            _Request(
                operation=operation,
                method="PATCH",
                path=f"/repos/{owner}/{repository_name}/issues/comments/{comment_id}",
                bearer=token,
                expected_status=httpx.codes.OK,
                json_body={"body": body},
            )
        )
        return self._review_comment_response(response, operation)

    def _review_comment_next_path(
        self,
        response: httpx.Response,
        *,
        expected_path: str,
        operation: GitHubOperation,
    ) -> str | None:
        next_link = response.links.get("next")
        if next_link is None:
            return None
        raw_url = next_link.get("url")
        if not isinstance(raw_url, str):
            raise GitHubError(operation)
        url = httpx.URL(raw_url)
        base_url = self._http.base_url
        if (
            url.scheme != base_url.scheme
            or url.host != base_url.host
            or url.port != base_url.port
            or url.path != expected_path
            or url.fragment
        ):
            raise GitHubError(operation)
        return str(url)

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
        token = self.installation_token(
            repository=self._repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.PULL_REQUEST_READ
        owner, repository_name = self._repository.split("/", maxsplit=1)
        response = self._request(
            _Request(
                operation=operation,
                method="GET",
                path=f"/repos/{owner}/{repository_name}/pulls/{pr_number}",
                bearer=token,
                expected_status=httpx.codes.OK,
            )
        )
        payload = self._validate_response(_PullRequestResponse, response, operation)
        if not isinstance(payload, _PullRequestResponse):
            raise GitHubError(operation)
        return ReviewRequest(
            repository=self._repository,
            pr_number=payload.number,
            installation_id=installation_id,
            base_sha=payload.base.sha,
            head_sha=payload.head.sha,
            title=payload.title,
            description=bound_description(payload.body),
        )

    def _review_comment_response(
        self,
        response: httpx.Response,
        operation: GitHubOperation,
    ) -> ReviewComment:
        payload = self._validate_response(ReviewComment, response, operation)
        if not isinstance(payload, ReviewComment):
            raise GitHubError(operation)
        return payload
