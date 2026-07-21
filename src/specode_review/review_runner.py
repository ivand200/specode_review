from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from specode_review.configuration import CANDIDATE_OUTPUT_MAX_BYTES, ReviewLimits
from specode_review.core import (
    CandidateAcceptance,
    CandidateContract,
    GitHubRepository,
    ReviewContext,
    Reviewer,
)
from specode_review.errors import FailureCategory, ReviewError
from specode_review.github import (
    GitHubError,
    GitHubOperation,
    ReviewComment,
    ReviewCommentGateway,
)
from specode_review.lifecycle_evidence import emit_lifecycle_evidence
from specode_review.models import ReviewRequest
from specode_review.publishing import (
    PublicationDisposition,
    PublicationReceipt,
    owned_revision_comments,
    publish_review_result,
)
from specode_review.resources import AttemptResources, ReviewResourceManager

_GITHUB_NOT_FOUND = 404


class PreflightOutcome(StrEnum):
    READY = "ready"
    ALREADY_REVIEWED = "already_reviewed"
    NOT_AUTHORIZED = "not_authorized"


class ReviewCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    review_status: Literal["issues_found", "no_important_issues"]
    finding_count: int = Field(ge=0, le=5, strict=True)
    publication: PublicationDisposition
    comment_id: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def status_matches_finding_count(self) -> "ReviewCompletion":
        expected = "issues_found" if self.finding_count else "no_important_issues"
        if self.review_status != expected:
            message = "review status must be derived from the finding count"
            raise ValueError(message)
        return self


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


class _RunGitHubClient(_PreflightGitHubClient, ReviewCommentGateway, Protocol):
    def installation_token(self, *, repository: str, installation_id: int) -> str: ...


class _CandidateAdapter(Protocol):
    def produce(self, context: ReviewContext, contract: CandidateContract) -> bytes: ...


class ReviewRunner:
    def __init__(  # noqa: PLR0913 - configured deep Module owns these fixed policies.
        self,
        *,
        github_client_factory: Callable[[str], _RunGitHubClient],
        resource_manager: ReviewResourceManager | None = None,
        candidate_adapter_factory: Callable[[AttemptResources], _CandidateAdapter] | None = None,
        source_repository: Path | None = None,
        limits: ReviewLimits | None = None,
        candidate_output_max_bytes: int = CANDIDATE_OUTPUT_MAX_BYTES,
    ) -> None:
        self._github_client_factory = github_client_factory
        self._resource_manager = resource_manager
        self._candidate_adapter_factory = candidate_adapter_factory
        self._source_repository = source_repository
        self._limits = limits or ReviewLimits()
        self._candidate_output_max_bytes = candidate_output_max_bytes

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
            normalized_request = request.model_copy(update={"repository": repository})
            if owned_revision_comments(request=normalized_request, gateway=github):
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

    def run(self, request: ReviewRequest, attempt_id: str) -> ReviewCompletion:
        resource_manager = self._resource_manager
        candidate_adapter_factory = self._candidate_adapter_factory
        if resource_manager is None or candidate_adapter_factory is None:
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="attempt_construction",
            ) from None
        stage = "attempt_construction"
        github: _RunGitHubClient | None = None
        resources: AttemptResources | None = None
        cleanup_confirmed = False
        failure: ReviewError | None = None
        completion: ReviewCompletion | None = None
        try:
            normalized_request = request.model_copy(
                update={"repository": request.repository.lower()}
            )
            resources = resource_manager.for_attempt(attempt_id)
            execution_github = self._github_client_factory(normalized_request.repository)
            github = execution_github
            adapter = candidate_adapter_factory(resources)
            reviewer = Reviewer(
                repository=normalized_request.repository,
                resources=resources,
                candidate_acceptance=CandidateAcceptance(
                    adapter=adapter,
                    max_bytes=self._candidate_output_max_bytes,
                ),
                source_repository=(
                    self._source_repository
                    if self._source_repository is not None
                    else GitHubRepository(credentials=execution_github)
                ),
                limits=self._limits,
            )

            stage = "review"
            result = reviewer.review(normalized_request)
            stage = "cleanup"
            resource_manager.cleanup(attempt_id)
            cleanup_confirmed = True
            stage = "publication"
            receipt = publish_review_result(
                request=normalized_request,
                result=result,
                gateway=execution_github,
            )
            completion = _completion(result.status, len(result.findings), receipt)
        except Exception as error:  # noqa: BLE001 - normalize the transaction boundary.
            failure = _normalized_failure(error, stage=stage)
        finally:
            if resources is not None and not cleanup_confirmed:
                try:
                    resource_manager.cleanup(attempt_id)
                    cleanup_confirmed = True
                except Exception as error:  # noqa: BLE001 - cleanup overrides earlier failure.
                    failure = _normalized_failure(error, stage="cleanup")
            if github is not None:
                try:
                    github.close()
                except Exception as error:  # noqa: BLE001 - close every call-local client.
                    if failure is None:
                        failure = _normalized_failure(error, stage="client_cleanup")

        emit_lifecycle_evidence(
            request,
            "cleanup",
            attempt_id=attempt_id,
            cleanup_outcome=(
                "not_required"
                if resources is None
                else "confirmed"
                if cleanup_confirmed
                else "failed"
            ),
        )
        emit_lifecycle_evidence(
            request,
            "publication",
            attempt_id=attempt_id,
            publication_disposition=(
                completion.publication.value
                if completion is not None
                else "failed"
                if failure is not None and failure.stage == "publication"
                else "suppressed"
            ),
        )
        if failure is not None:
            raise failure from None
        if completion is None:
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="review",
            ) from None
        return completion


def _completion(
    review_status: Literal["issues_found", "no_important_issues"],
    finding_count: int,
    receipt: PublicationReceipt,
) -> ReviewCompletion:
    return ReviewCompletion(
        review_status=review_status,
        finding_count=finding_count,
        publication=receipt.disposition,
        comment_id=receipt.comment_id,
    )


def _normalized_failure(error: Exception, *, stage: str) -> ReviewError:
    if isinstance(error, ReviewError):
        return ReviewError(error.category, stage=error.stage)
    if isinstance(error, TimeoutError):
        return ReviewError(FailureCategory.TIMEOUT, stage=stage)
    return ReviewError(FailureCategory.REVIEW_FAILURE, stage=stage)
