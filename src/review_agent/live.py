from dataclasses import dataclass

from review_agent.github import (
    CheckRunConclusion,
    CheckRunStatus,
    GitHubAppClient,
    derive_review_identity,
)
from review_agent.models import ReviewRequest
from review_agent.publishing import owned_revision_comments

FINDINGS_COMPLETE_TITLE = "Review complete — findings published"


class LiveProfilePreconditionError(Exception):
    """The accepted revision is not safe for a live rollout profile."""


class LiveProfileEvidenceError(Exception):
    """The current attempt did not produce the required rollout evidence."""


@dataclass(frozen=True)
class LiveReviewEvidence:
    """Identifiers confirmed by the successful Checkpoint C evidence gate."""

    check_run_id: int
    comment_id: int


def require_fresh_live_review(
    *,
    request: ReviewRequest,
    github: GitHubAppClient,
) -> None:
    identity = derive_review_identity(request)
    owned_check_runs = tuple(
        check_run
        for check_run in github.list_check_runs(
            identity=identity,
            installation_id=request.installation_id,
        )
        if github.is_owned_check_run(check_run, identity=identity)
    )
    if owned_check_runs:
        message = (
            "live profile requires no application-owned Check Run for this review identity; "
            "manually prepare a fresh accepted base/head revision"
        )
        raise LiveProfilePreconditionError(message)
    if owned_revision_comments(request=request, gateway=github):
        message = (
            "live profile requires no exact-marker application-owned comment for this review "
            "identity; manually prepare a fresh accepted base/head revision"
        )
        raise LiveProfilePreconditionError(message)


def verify_live_review_evidence(
    *,
    request: ReviewRequest,
    github: GitHubAppClient,
    expected_finding: str,
    forbidden_texts: tuple[str, ...],
) -> LiveReviewEvidence:
    identity = derive_review_identity(request)
    owned_check_runs = tuple(
        check_run
        for check_run in github.list_check_runs(
            identity=identity,
            installation_id=request.installation_id,
        )
        if github.is_owned_check_run(check_run, identity=identity)
    )
    if (
        len(owned_check_runs) != 1
        or owned_check_runs[0].status is not CheckRunStatus.COMPLETED
        or owned_check_runs[0].conclusion is not CheckRunConclusion.NEUTRAL
        or owned_check_runs[0].output.title != FINDINGS_COMPLETE_TITLE
    ):
        message = "checkpoint C requires exactly one completed neutral findings Check Run"
        raise LiveProfileEvidenceError(message)

    comments = owned_revision_comments(request=request, gateway=github)
    if len(comments) != 1:
        message = "checkpoint C requires exactly one exact-marker application-owned comment"
        raise LiveProfileEvidenceError(message)
    comment = comments[0]
    if expected_finding.casefold() not in comment.body.casefold():
        message = "checkpoint C comment does not contain the expected finding"
        raise LiveProfileEvidenceError(message)
    if any(forbidden in comment.body for forbidden in forbidden_texts):
        message = "checkpoint C comment contains forbidden repository text"
        raise LiveProfileEvidenceError(message)
    return LiveReviewEvidence(
        check_run_id=owned_check_runs[0].id,
        comment_id=comment.id,
    )
