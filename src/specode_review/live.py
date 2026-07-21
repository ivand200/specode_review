import re
from dataclasses import dataclass
from typing import Protocol

from specode_review.accepted_revision import AcceptedRevision
from specode_review.github import ReviewCommentGateway
from specode_review.models import ReviewRequest
from specode_review.publishing import owned_revision_comments

_ALLOWED_SEVERITY = re.compile(
    r"^- Severity: `+ (?:blocking|important) `+$",
    re.MULTILINE,
)


class LiveProfilePreconditionError(Exception):
    """The accepted revision is not safe for a live rollout profile."""


class LiveProfileEvidenceError(Exception):
    """The current attempt did not produce the required rollout evidence."""


@dataclass(frozen=True)
class LiveReviewEvidence:
    """Identifiers confirmed by the successful live-review evidence gate."""

    comment_id: int


class _LiveGitHub(ReviewCommentGateway, Protocol):
    pass


def require_fresh_live_review(
    *,
    request: ReviewRequest,
    github: _LiveGitHub,
    expected: AcceptedRevision,
) -> None:
    if AcceptedRevision.from_review_request(request) != expected:
        message = "live pull request does not match the prepared accepted revision"
        raise LiveProfilePreconditionError(message)

    if owned_revision_comments(request=request, gateway=github):
        message = (
            "live profile requires no exact-marker application-owned comment for this review "
            "identity; manually prepare a fresh accepted base/head revision"
        )
        raise LiveProfilePreconditionError(message)


def verify_live_review_evidence(  # noqa: PLR0913 - one evidence gate.
    *,
    request: ReviewRequest,
    github: _LiveGitHub,
    expected_finding: str,
    expected_path: str | None = None,
    expected_line: int | None = None,
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
    if expected_path is not None:
        expected_location = (
            f"{expected_path}:{expected_line}"
            if expected_line is not None
            else expected_path
        )
        raw_sections = comment.body.split("### Finding ")[1:]
        sections = (
            tuple(f"### Finding {section}" for section in raw_sections)
            if raw_sections
            else (comment.body,)
        )
        matching_sections = tuple(
            section
            for section in sections
            if expected_finding.casefold() in section.casefold()
            and expected_location.casefold() in section.casefold()
        )
        if not matching_sections:
            message = "live review comment does not contain the grounded expected location"
            raise LiveProfileEvidenceError(message)
        if not any(_ALLOWED_SEVERITY.search(section) for section in matching_sections):
            message = "live review seeded finding does not have an allowed severity"
            raise LiveProfileEvidenceError(message)
    elif _ALLOWED_SEVERITY.search(comment.body) is None:
        message = "live review comment does not contain an allowed finding severity"
        raise LiveProfileEvidenceError(message)
    if any(forbidden in comment.body for forbidden in forbidden_texts):
        message = "live review comment contains forbidden repository text"
        raise LiveProfileEvidenceError(message)
    return LiveReviewEvidence(comment_id=comment.id)
