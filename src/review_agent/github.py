import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from review_agent.deadline import remaining_review_time
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import RepositoryName, ReviewRequest, Sha, bound_description

GITHUB_API_VERSION = "2026-03-10"
CHECK_RUN_NAME = "Review Agent"
CHECK_RUN_PAGE_SIZE = 100
CHECK_RUN_MAX_PAGES = 10
REVIEW_COMMENT_PAGE_SIZE = 100
REVIEW_COMMENT_MAX_PAGES = 10
GITHUB_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
MAX_REVIEW_FINDINGS = 5
_EXTERNAL_ID_PREFIX = "review-agent:v1:"

ExternalReviewId = Annotated[
    str,
    StringConstraints(pattern=r"^review-agent:v1:[0-9a-f]{64}$", max_length=80),
]
SafeOutputDetail = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$"),
]
_SAFE_OUTPUT_DETAIL_PATTERN = re.compile(r"^[a-z0-9_.-]{1,64}$")


class GitHubOperation(StrEnum):
    CHECK_RUN_CREATE = "check_run_create"
    CHECK_RUN_LIST = "check_run_list"
    CHECK_RUN_READ = "check_run_read"
    CHECK_RUN_UPDATE = "check_run_update"
    INSTALLATION_READ = "installation_read"
    INSTALLATION_TOKEN = "installation_token"  # noqa: S105 - normalized operation name.
    PULL_REQUEST_READ = "pull_request_read"
    PUBLICATION = "publication"
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


class ReviewIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    pr_number: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha
    external_id: ExternalReviewId

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("base_sha", "head_sha", mode="before")
    @classmethod
    def normalize_sha(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def external_id_matches_identity(self) -> "ReviewIdentity":
        expected = _derive_external_id(
            repository=self.repository,
            pr_number=self.pr_number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        if self.external_id != expected:
            message = "external_id does not match the review identity"
            raise ValueError(message)
        return self


class CheckRunStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CheckRunConclusion(StrEnum):
    SUCCESS = "success"
    NEUTRAL = "neutral"


class CheckRunOutputKind(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CLEAN = "clean"
    FINDINGS = "findings"
    TECHNICAL_FAILURE = "technical_failure"
    TIMEOUT = "timeout"
    PUBLICATION_UNKNOWN = "publication_unknown"


class CheckRunAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=20)
    description: str = Field(min_length=1, max_length=40)
    identifier: str = Field(min_length=1, max_length=20, pattern=r"^[a-z0-9_]+$")


class CheckRunOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=4_096)


class CheckRunPresentation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CheckRunStatus
    conclusion: CheckRunConclusion | None
    output: CheckRunOutput
    actions: tuple[CheckRunAction, ...] = Field(max_length=1)

    @model_validator(mode="after")
    def status_matches_conclusion(self) -> "CheckRunPresentation":
        if self.status is CheckRunStatus.COMPLETED and self.conclusion is None:
            message = "completed Check Runs require a conclusion"
            raise ValueError(message)
        if self.status is not CheckRunStatus.COMPLETED and self.conclusion is not None:
            message = "non-completed Check Runs cannot have a conclusion"
            raise ValueError(message)
        if self.actions and self.conclusion is not CheckRunConclusion.NEUTRAL:
            message = "retry actions require an incomplete neutral conclusion"
            raise ValueError(message)
        return self


class _CheckRunApp(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0, strict=True)


class CheckRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(gt=0, strict=True)
    name: str = Field(min_length=1, max_length=100, strict=True)
    head_sha: Sha
    external_id: str | None = Field(default=None, max_length=255)
    status: CheckRunStatus
    conclusion: CheckRunConclusion | None
    app: _CheckRunApp
    output: CheckRunOutput
    actions: tuple[CheckRunAction, ...] = Field(default=(), max_length=1)

    @model_validator(mode="after")
    def status_matches_conclusion(self) -> "CheckRun":
        if self.status is CheckRunStatus.COMPLETED and self.conclusion is None:
            message = "completed Check Runs require a conclusion"
            raise ValueError(message)
        if self.status is not CheckRunStatus.COMPLETED and self.conclusion is not None:
            message = "non-completed Check Runs cannot have a conclusion"
            raise ValueError(message)
        if self.actions and self.conclusion is not CheckRunConclusion.NEUTRAL:
            message = "requested actions require an incomplete neutral conclusion"
            raise ValueError(message)
        return self


class _CheckRunListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    total_count: int = Field(ge=0, strict=True)
    check_runs: tuple[CheckRun, ...] = Field(max_length=CHECK_RUN_PAGE_SIZE)


def _derive_external_id(
    *,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> str:
    canonical = f"v1\n{repository.lower()}\n{pr_number}\n{base_sha.lower()}\n{head_sha.lower()}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{_EXTERNAL_ID_PREFIX}{digest}"


def derive_review_identity(request: ReviewRequest) -> ReviewIdentity:
    repository = request.repository.lower()
    base_sha = request.base_sha.lower()
    head_sha = request.head_sha.lower()
    return ReviewIdentity(
        repository=repository,
        pr_number=request.pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        external_id=_derive_external_id(
            repository=repository,
            pr_number=request.pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
        ),
    )


def _accepted_range(identity: ReviewIdentity) -> str:
    return f"{identity.base_sha[:12]}..{identity.head_sha[:12]}"


def _retry_action() -> tuple[CheckRunAction, ...]:
    return (
        CheckRunAction(
            label="Retry review",
            description="Retry this incomplete advisory review.",
            identifier="retry_review",
        ),
    )


def _render_active_presentation(
    output_kind: CheckRunOutputKind,
    *,
    identity: ReviewIdentity,
) -> CheckRunPresentation:
    accepted_range = _accepted_range(identity)
    if output_kind is CheckRunOutputKind.QUEUED:
        return CheckRunPresentation(
            status=CheckRunStatus.QUEUED,
            conclusion=None,
            output=CheckRunOutput(
                title="Review queued",
                summary=(
                    "Review Agent queued an advisory review for accepted range "
                    f"{accepted_range}."
                ),
            ),
            actions=(),
        )
    return CheckRunPresentation(
        status=CheckRunStatus.IN_PROGRESS,
        conclusion=None,
        output=CheckRunOutput(
            title="Review in progress",
            summary=(
                "Review Agent is reviewing accepted range "
                f"{accepted_range}. Detailed findings publish as a pull request comment."
            ),
        ),
        actions=(),
    )


def _render_reviewed_presentation(
    output_kind: CheckRunOutputKind,
    *,
    identity: ReviewIdentity,
    finding_count: int | None,
) -> CheckRunPresentation:
    accepted_range = _accepted_range(identity)
    if output_kind is CheckRunOutputKind.CLEAN:
        if finding_count != 0:
            message = "clean presentation requires finding_count=0"
            raise ValueError(message)
        return CheckRunPresentation(
            status=CheckRunStatus.COMPLETED,
            conclusion=CheckRunConclusion.SUCCESS,
            output=CheckRunOutput(
                title="Review complete — no important findings",
                summary=(
                    "Review Agent completed the advisory review for accepted range "
                    f"{accepted_range} with no important findings."
                ),
            ),
            actions=(),
        )
    if finding_count is not None and not 1 <= finding_count <= MAX_REVIEW_FINDINGS:
        message = "findings presentation requires between one and five findings"
        raise ValueError(message)
    finding_summary = (
        "advisory findings"
        if finding_count is None
        else f"{finding_count} advisory finding(s)"
    )
    return CheckRunPresentation(
        status=CheckRunStatus.COMPLETED,
        conclusion=CheckRunConclusion.NEUTRAL,
        output=CheckRunOutput(
            title="Review complete — findings published",
            summary=(
                f"Review Agent published {finding_summary} for "
                "accepted range "
                f"{accepted_range} in the pull request comment."
            ),
        ),
        actions=(),
    )


def _render_incomplete_presentation(
    output_kind: CheckRunOutputKind,
    *,
    identity: ReviewIdentity,
    failure_stage: SafeOutputDetail | None,
    failure_category: SafeOutputDetail | None,
) -> CheckRunPresentation:
    accepted_range = _accepted_range(identity)
    if output_kind is CheckRunOutputKind.TIMEOUT:
        title = "Review incomplete — timeout"
        summary = f"The advisory review for accepted range {accepted_range} timed out."
    elif output_kind is CheckRunOutputKind.PUBLICATION_UNKNOWN:
        title = "Review incomplete — publication unknown"
        summary = (
            f"The advisory review for accepted range {accepted_range} ended before publication "
            "could be confirmed. Retrying may duplicate a previously published comment."
        )
    elif output_kind is CheckRunOutputKind.TECHNICAL_FAILURE:
        if failure_stage is None or failure_category is None:
            message = "technical failure presentation requires normalized stage and category"
            raise ValueError(message)
        if _SAFE_OUTPUT_DETAIL_PATTERN.fullmatch(failure_stage) is None:
            message = "failure_stage must be a normalized application-owned value"
            raise ValueError(message)
        if _SAFE_OUTPUT_DETAIL_PATTERN.fullmatch(failure_category) is None:
            message = "failure_category must be a normalized application-owned value"
            raise ValueError(message)
        safe_stage = failure_stage
        safe_category = failure_category
        title = "Review incomplete — technical failure"
        summary = (
            f"The advisory review for accepted range {accepted_range} stopped during "
            f"{safe_stage} ({safe_category})."
        )
    else:
        message = "unsupported Check Run output kind"
        raise ValueError(message)
    return CheckRunPresentation(
        status=CheckRunStatus.COMPLETED,
        conclusion=CheckRunConclusion.NEUTRAL,
        output=CheckRunOutput(
            title=title,
            summary=f"{summary} Use Retry review to start a new attempt.",
        ),
        actions=_retry_action(),
    )


def render_check_run_presentation(
    output_kind: CheckRunOutputKind,
    *,
    identity: ReviewIdentity,
    finding_count: int | None = None,
    failure_stage: SafeOutputDetail | None = None,
    failure_category: SafeOutputDetail | None = None,
) -> CheckRunPresentation:
    if output_kind in {CheckRunOutputKind.QUEUED, CheckRunOutputKind.RUNNING}:
        return _render_active_presentation(output_kind, identity=identity)
    if output_kind in {CheckRunOutputKind.CLEAN, CheckRunOutputKind.FINDINGS}:
        return _render_reviewed_presentation(
            output_kind,
            identity=identity,
            finding_count=finding_count,
        )
    return _render_incomplete_presentation(
        output_kind,
        identity=identity,
        failure_stage=failure_stage,
        failure_category=failure_category,
    )


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
                        "checks": "write",
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

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        token = self.installation_token(
            repository=repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.PUBLICATION
        owner, repository_name = repository.split("/", maxsplit=1)
        self._request(
            _Request(
                operation=operation,
                method="POST",
                path=f"/repos/{owner}/{repository_name}/issues/{pr_number}/comments",
                bearer=token,
                expected_status=httpx.codes.CREATED,
                json_body={"body": body},
            )
        )

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

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        self._require_identity_repository(identity)
        token = self.installation_token(
            repository=self._repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.CHECK_RUN_LIST
        owner, repository_name = self._repository.split("/", maxsplit=1)
        check_runs: list[CheckRun] = []
        for page in range(1, CHECK_RUN_MAX_PAGES + 1):
            query = urlencode(
                {
                    "check_name": CHECK_RUN_NAME,
                    "app_id": str(self._app_id),
                    "filter": "all",
                    "per_page": str(CHECK_RUN_PAGE_SIZE),
                    "page": str(page),
                }
            )
            response = self._request(
                _Request(
                    operation=operation,
                    method="GET",
                    path=(
                        f"/repos/{owner}/{repository_name}/commits/{identity.head_sha}/check-runs"
                        f"?{query}"
                    ),
                    bearer=token,
                    expected_status=httpx.codes.OK,
                )
            )
            payload = self._validate_response(_CheckRunListResponse, response, operation)
            if not isinstance(payload, _CheckRunListResponse):
                raise GitHubError(operation)
            check_runs.extend(payload.check_runs)
            if len(check_runs) >= payload.total_count:
                return tuple(check_runs)
            if len(payload.check_runs) < CHECK_RUN_PAGE_SIZE:
                raise GitHubError(operation)
        raise GitHubError(operation)

    def create_check_run(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> CheckRun:
        self._require_identity_repository(identity)
        token = self.installation_token(
            repository=self._repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.CHECK_RUN_CREATE
        owner, repository_name = self._repository.split("/", maxsplit=1)
        queued = render_check_run_presentation(CheckRunOutputKind.QUEUED, identity=identity)
        response = self._request(
            _Request(
                operation=operation,
                method="POST",
                path=f"/repos/{owner}/{repository_name}/check-runs",
                bearer=token,
                expected_status=httpx.codes.CREATED,
                json_body={
                    "name": CHECK_RUN_NAME,
                    "head_sha": identity.head_sha,
                    "external_id": identity.external_id,
                    "status": queued.status.value,
                    "output": queued.output.model_dump(mode="json"),
                },
            )
        )
        return self._check_run_response(response, operation)

    def get_check_run(self, *, check_run_id: int, installation_id: int) -> CheckRun:
        if check_run_id < 1:
            message = "check_run_id must be positive"
            raise ValueError(message)
        token = self.installation_token(
            repository=self._repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.CHECK_RUN_READ
        owner, repository_name = self._repository.split("/", maxsplit=1)
        response = self._request(
            _Request(
                operation=operation,
                method="GET",
                path=f"/repos/{owner}/{repository_name}/check-runs/{check_run_id}",
                bearer=token,
                expected_status=httpx.codes.OK,
            )
        )
        return self._check_run_response(response, operation)

    def update_check_run(
        self,
        *,
        check_run_id: int,
        installation_id: int,
        presentation: CheckRunPresentation,
    ) -> CheckRun:
        if check_run_id < 1:
            message = "check_run_id must be positive"
            raise ValueError(message)
        token = self.installation_token(
            repository=self._repository,
            installation_id=installation_id,
        )
        operation = GitHubOperation.CHECK_RUN_UPDATE
        owner, repository_name = self._repository.split("/", maxsplit=1)
        body: dict[str, Any] = {
            "status": presentation.status.value,
            "output": presentation.output.model_dump(mode="json"),
            "actions": [action.model_dump(mode="json") for action in presentation.actions],
        }
        if presentation.conclusion is not None:
            body["conclusion"] = presentation.conclusion.value
        response = self._request(
            _Request(
                operation=operation,
                method="PATCH",
                path=f"/repos/{owner}/{repository_name}/check-runs/{check_run_id}",
                bearer=token,
                expected_status=httpx.codes.OK,
                json_body=body,
            )
        )
        return self._check_run_response(response, operation)

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
        return (
            identity.repository == self._repository.lower()
            and check_run.app.id == self._app_id
            and check_run.name == CHECK_RUN_NAME
            and check_run.head_sha.lower() == identity.head_sha
            and check_run.external_id == identity.external_id
        )

    def _require_identity_repository(self, identity: ReviewIdentity) -> None:
        if identity.repository != self._repository.lower():
            message = "identity repository does not match the configured GitHub repository"
            raise ValueError(message)

    def _check_run_response(
        self,
        response: httpx.Response,
        operation: GitHubOperation,
    ) -> CheckRun:
        payload = self._validate_response(CheckRun, response, operation)
        if not isinstance(payload, CheckRun):
            raise GitHubError(operation)
        return payload

    def _review_comment_response(
        self,
        response: httpx.Response,
        operation: GitHubOperation,
    ) -> ReviewComment:
        payload = self._validate_response(ReviewComment, response, operation)
        if not isinstance(payload, ReviewComment):
            raise GitHubError(operation)
        return payload
