import pytest

from review_agent.github import (
    CheckRun,
    CheckRunConclusion,
    CheckRunStatus,
    ReviewComment,
    ReviewCommentApp,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.live import LiveProfilePreconditionError, require_fresh_live_review
from review_agent.models import AcceptedRevision, ReviewRequest


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


def _owned_check_run(identity: ReviewIdentity) -> CheckRun:
    return CheckRun.model_validate(
        {
            "id": 91,
            "name": "Review Agent",
            "head_sha": identity.head_sha,
            "external_id": identity.external_id,
            "status": CheckRunStatus.COMPLETED,
            "conclusion": CheckRunConclusion.SUCCESS,
            "app": {"id": 12345},
            "output": {"title": "old result", "summary": "old result"},
        }
    )


class FreshnessGateway:
    def __init__(
        self,
        *,
        check_runs: tuple[CheckRun, ...] = (),
        comments: tuple[ReviewComment, ...] = (),
    ) -> None:
        self.check_runs = check_runs
        self.comments = comments
        self.calls: list[str] = []

    @property
    def app_id(self) -> int:
        return 12345

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        assert identity == derive_review_identity(_request())
        assert installation_id == 23
        self.calls.append("list_check_runs")
        return self.check_runs

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
        self.calls.append("is_owned_check_run")
        return (
            check_run.app.id == self.app_id
            and check_run.external_id == identity.external_id
            and check_run.head_sha == identity.head_sha
        )

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


def test_existing_owned_check_run_rejects_live_fixture_before_comment_scan() -> None:
    request = _request()
    gateway = FreshnessGateway(check_runs=(_owned_check_run(derive_review_identity(request)),))

    with pytest.raises(
        LiveProfilePreconditionError,
        match="manually prepare a fresh accepted base/head revision",
    ):
        require_fresh_live_review(
            request=request,
            github=gateway,
            expected=_expected_identity(request),
        )

    assert gateway.calls == ["list_check_runs", "is_owned_check_run"]


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

    assert gateway.calls == ["list_check_runs", "list_review_comments"]


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

    assert gateway.calls == ["list_check_runs", "list_review_comments"]


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
