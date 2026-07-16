from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_agent.models import ReviewRequest, Sha, bound_description

GITHUB_API_VERSION = "2026-03-10"


class GitHubOperation(StrEnum):
    INSTALLATION_READ = "installation_read"
    INSTALLATION_TOKEN = "installation_token"  # noqa: S105 - normalized operation name.
    PULL_REQUEST_READ = "pull_request_read"
    PUBLICATION = "publication"


class GitHubError(Exception):
    def __init__(
        self,
        operation: GitHubOperation,
        *,
        status_code: int | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        status_suffix = f" with status {status_code}" if status_code is not None else ""
        super().__init__(f"GitHub {operation.value} failed{status_suffix}")


@dataclass(frozen=True, slots=True)
class _Request:
    operation: GitHubOperation
    method: Literal["GET", "POST"]
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
        try:
            if request.json_body is None:
                response = self._http.request(request.method, request.path, headers=headers)
            else:
                response = self._http.request(
                    request.method,
                    request.path,
                    headers=headers,
                    json=request.json_body,
                )
        except httpx.HTTPError:
            raise GitHubError(request.operation) from None
        if response.status_code != request.expected_status:
            raise GitHubError(
                request.operation,
                status_code=response.status_code,
            ) from None
        return response

    @staticmethod
    def _validate_response(
        model: type[BaseModel],
        response: httpx.Response,
        operation: GitHubOperation,
    ) -> BaseModel:
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
                    "permissions": {"contents": "read", "pull_requests": "write"},
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
