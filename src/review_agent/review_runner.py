import hashlib
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from review_agent.errors import FailureCategory, ReviewError
from review_agent.github import GitHubError, GitHubOperation, ReviewComment
from review_agent.models import ReviewRequest

_GITHUB_NOT_FOUND = 404


class PreflightOutcome(StrEnum):
    READY = "ready"
    ALREADY_REVIEWED = "already_reviewed"
    NOT_AUTHORIZED = "not_authorized"


class _PreflightGitHubClient(Protocol):
    @property
    def app_id(self) -> int: ...

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]: ...

    def close(self) -> None: ...


class ReviewRunner:
    def __init__(
        self,
        *,
        github_client_factory: Callable[[str], _PreflightGitHubClient],
    ) -> None:
        self._github_client_factory = github_client_factory

    def preflight(self, request: ReviewRequest) -> PreflightOutcome:
        repository = request.repository.lower()
        try:
            github = self._github_client_factory(repository)
        except Exception:  # noqa: BLE001 - normalize the true-external adapter boundary.
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="preflight",
            ) from None
        try:
            comments = github.list_review_comments(
                repository=repository,
                pr_number=request.pr_number,
                installation_id=request.installation_id,
            )
            marker = _review_marker(request)
            if any(
                _is_owned_revision_comment(comment, marker=marker, app_id=github.app_id)
                for comment in comments
            ):
                return PreflightOutcome.ALREADY_REVIEWED
        except GitHubError as error:
            if (
                error.operation is GitHubOperation.INSTALLATION_TOKEN
                and error.status_code == _GITHUB_NOT_FOUND
            ):
                return PreflightOutcome.NOT_AUTHORIZED
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="preflight",
            ) from None
        except ReviewError:
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="preflight",
            ) from None
        except Exception:  # noqa: BLE001 - normalize the true-external adapter boundary.
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="preflight",
            ) from None
        else:
            return PreflightOutcome.READY
        finally:
            try:
                github.close()
            except Exception:  # noqa: BLE001 - normalize the call-local cleanup boundary.
                raise ReviewError(
                    FailureCategory.REVIEW_FAILURE,
                    stage="preflight",
                ) from None


def _review_marker(request: ReviewRequest) -> str:
    canonical = (
        f"v1\n{request.repository.lower()}\n{request.pr_number}\n"
        f"{request.base_sha.lower()}\n{request.head_sha.lower()}"
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"<!-- specode-review:v1:{digest} -->"


def _is_owned_revision_comment(
    comment: ReviewComment,
    *,
    marker: str,
    app_id: int,
) -> bool:
    return (
        comment.body.endswith(f"\n{marker}\n")
        and comment.performed_via_github_app is not None
        and comment.performed_via_github_app.id == app_id
    )
