from dataclasses import dataclass

from specode_review.github import GitHubAppClient
from specode_review.models import AcceptedRevision, ReviewRequest
from specode_review.publishing import owned_revision_comments


class LiveProfilePreconditionError(Exception):
    """The accepted revision is not safe for a live rollout profile."""


class LiveProfileEvidenceError(Exception):
    """The current attempt did not produce the required rollout evidence."""


@dataclass(frozen=True)
class LiveReviewEvidence:
    """Identifiers confirmed by the successful live-review evidence gate."""

    comment_id: int


def require_fresh_live_review(
    *,
    request: ReviewRequest,
    github: GitHubAppClient,
    expected: AcceptedRevision,
) -> None:
    if (
        request.repository.casefold() != expected.repository.casefold()
        or request.pr_number != expected.pr_number
        or request.base_sha.casefold() != expected.base_sha.casefold()
        or request.head_sha.casefold() != expected.head_sha.casefold()
    ):
        message = "live pull request does not match the prepared accepted revision"
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
    comments = owned_revision_comments(request=request, gateway=github)
    if len(comments) != 1:
        message = "live review requires exactly one exact-marker application-owned comment"
        raise LiveProfileEvidenceError(message)
    comment = comments[0]
    if expected_finding.casefold() not in comment.body.casefold():
        message = "live review comment does not contain the expected finding"
        raise LiveProfileEvidenceError(message)
    if any(forbidden in comment.body for forbidden in forbidden_texts):
        message = "live review comment contains forbidden repository text"
        raise LiveProfileEvidenceError(message)
    return LiveReviewEvidence(comment_id=comment.id)
