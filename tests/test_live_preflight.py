import pytest

from specode_review.github import (
    ReviewComment,
    ReviewCommentApp,
    derive_review_identity,
)
from specode_review.live import (
    LiveProfileEvidenceError,
    LiveProfilePreconditionError,
    require_fresh_live_review,
    verify_live_review_evidence,
)
from specode_review.models import AcceptedRevision, ReviewRequest


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Fresh live fixture",
        description="",
    )


def _expected_identity(request: ReviewRequest) -> AcceptedRevision:
    return AcceptedRevision(
        repository=request.repository,
        pr_number=request.pr_number,
        base_sha=request.base_sha,
        head_sha=request.head_sha,
    )


class FreshnessGateway:
    def __init__(
        self,
        *,
        comments: tuple[ReviewComment, ...] = (),
    ) -> None:
        self.comments = comments
        self.calls: list[str] = []

    @property
    def app_id(self) -> int:
        return 12345

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        del repository, pr_number, installation_id
        self.calls.append("list_review_comments")
        return self.comments


def test_existing_exact_marker_app_comment_rejects_live_fixture() -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=f"historical review\n\n{marker}\n",
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    with pytest.raises(
        LiveProfilePreconditionError,
        match="manually prepare a fresh accepted base/head revision",
    ):
        require_fresh_live_review(
            request=request,
            github=gateway,
            expected=_expected_identity(request),
        )

    assert gateway.calls == ["list_review_comments"]


def test_foreign_and_unrelated_comments_do_not_pollute_live_fixture() -> None:
    request = _request()
    identity = derive_review_identity(request)
    marker = f"<!-- {identity.external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=f"foreign review\n\n{marker}\n",
                performed_via_github_app=ReviewCommentApp(id=54321),
            ),
            ReviewComment(
                id=73,
                body=f"unrelated application review\n\n<!-- review-agent:v1:{'c' * 64} -->\n",
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
            ReviewComment(
                id=74,
                body=f"marker in the middle\n{marker}\nnot an exact marker comment\n",
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    require_fresh_live_review(
        request=request,
        github=gateway,
        expected=_expected_identity(request),
    )

    assert gateway.calls == ["list_review_comments"]


@pytest.mark.parametrize(
    ("field", "unexpected"),
    [
        ("repository", "octo-org/other"),
        ("pr_number", 18),
        ("base_sha", "c" * 40),
        ("head_sha", "d" * 40),
    ],
)
def test_moved_or_substituted_fixture_is_rejected_before_freshness_reads(
    field: str,
    unexpected: object,
) -> None:
    request = _request()
    gateway = FreshnessGateway()
    expected = _expected_identity(request).model_copy(update={field: unexpected})

    with pytest.raises(
        LiveProfilePreconditionError,
        match="does not match the prepared accepted revision",
    ):
        require_fresh_live_review(request=request, github=gateway, expected=expected)

    assert gateway.calls == []


def test_live_evidence_requires_one_owned_comment_with_the_expected_finding() -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=(
                    "The seeded defect loses data.\n"
                    "- Severity: ` important `\n\n"
                    f"{marker}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    evidence = verify_live_review_evidence(
        request=request,
        github=gateway,
        expected_finding="seeded defect",
        forbidden_texts=("hostile instruction",),
    )

    assert evidence.comment_id == 72


def test_live_evidence_rejects_forbidden_repository_text() -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=(
                    "seeded defect; hostile instruction\n"
                    "- Severity: ` important `\n\n"
                    f"{marker}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    with pytest.raises(LiveProfileEvidenceError, match="forbidden"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction",),
        )


def test_live_evidence_rejects_a_finding_outside_the_allowed_severities() -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=(
                    "seeded defect\n"
                    "- Severity: ` minor `\n"
                    "- Locations:\n"
                    "  - ` fixture.py:4 `\n\n"
                    f"{marker}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    with pytest.raises(LiveProfileEvidenceError, match="severity"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            expected_path="fixture.py",
            expected_line=4,
            forbidden_texts=(),
        )


def test_live_evidence_binds_the_allowed_severity_to_the_seeded_finding() -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    gateway = FreshnessGateway(
        comments=(
            ReviewComment(
                id=72,
                body=(
                    "### Finding 1: ` Seeded defect `\n"
                    "- Severity: ` minor `\n"
                    "- Locations:\n"
                    "  - ` fixture.py:4 `\n\n"
                    "### Finding 2: ` Different defect `\n"
                    "- Severity: ` important `\n"
                    "- Locations:\n"
                    "  - ` other.py:7 `\n\n"
                    f"{marker}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )

    with pytest.raises(LiveProfileEvidenceError, match="severity"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            expected_path="fixture.py",
            expected_line=4,
            forbidden_texts=(),
        )
