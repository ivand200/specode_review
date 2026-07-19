import pytest

from review_agent.github import ReviewComment, ReviewCommentApp
from review_agent.models import DiffRange, Finding, Location, ReviewRequest, ReviewResult
from review_agent.publishing import (
    PublicationConsistencyError,
    PublicationDisposition,
    PublicationReceipt,
    publish_review_result,
    render_review_comment,
)


class ScriptedCommentGateway:
    def __init__(
        self,
        *,
        comments: tuple[ReviewComment, ...] = (),
        app_id: int = 12345,
    ) -> None:
        self._app_id = app_id
        self.comments = comments
        self.listed = 0
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []

    @property
    def app_id(self) -> int:
        return self._app_id

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        self.listed += 1
        assert (repository, pr_number, installation_id) == (
            "Octo-Org/Example",
            17,
            23,
        )
        return self.comments

    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        assert (repository, pr_number, installation_id) == (
            "Octo-Org/Example",
            17,
            23,
        )
        self.created.append(body)
        return ReviewComment(
            id=101,
            body=body,
            performed_via_github_app=ReviewCommentApp(id=self.app_id),
        )

    def update_review_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        assert (repository, installation_id) == ("Octo-Org/Example", 23)
        self.updated.append((comment_id, body))
        return ReviewComment(
            id=comment_id,
            body=body,
            performed_via_github_app=ReviewCommentApp(id=self.app_id),
        )


class FailingUpdateGateway(ScriptedCommentGateway):
    def update_review_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        del repository, comment_id, installation_id, body
        message = "definitive update failure"
        raise RuntimeError(message)


def _request(**updates: object) -> ReviewRequest:
    return ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Add feature",
    ).model_copy(update=updates)


def _result(**updates: object) -> ReviewResult:
    return ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=DiffRange(start_sha="c" * 40, end_sha="b" * 40),
        status="no_important_issues",
        findings=(),
    ).model_copy(update=updates)


def _findings_result() -> ReviewResult:
    finding = Finding(
        severity="important",
        title="Feature data can be lost",
        locations=(Location(path="feature.txt", line=1, description=None),),
        evidence="The new write replaces existing data.",
        impact="A user can lose saved data.",
        suggested_fix="Preserve and merge the existing data.",
    )
    return _result(status="issues_found", findings=(finding,))


def _expected_body(result: ReviewResult | None = None) -> str:
    marker = (
        "<!-- review-agent:v1:"
        "b3fdc634e74cf30721e4dc24158636348334fa1c133b44a74eb401e89db2119f -->"
    )
    return f"{render_review_comment(result or _result())}\n{marker}\n"


def test_missing_or_deleted_revision_comment_is_recreated_with_the_marker() -> None:
    gateway = ScriptedCommentGateway()

    receipt = publish_review_result(
        request=_request(),
        result=_result(),
        gateway=gateway,
    )

    assert gateway.created == [_expected_body()]
    assert gateway.updated == []
    assert receipt == PublicationReceipt(
        comment_id=101,
        disposition=PublicationDisposition.CREATED,
    )


def test_same_revision_with_the_current_owned_comment_reuses_it_without_a_write() -> None:
    current = ReviewComment(
        id=202,
        body=_expected_body(),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = ScriptedCommentGateway(comments=(current,))

    receipt = publish_review_result(
        request=_request(),
        result=_result(),
        gateway=gateway,
    )

    assert gateway.created == []
    assert gateway.updated == []
    assert receipt == PublicationReceipt(
        comment_id=202,
        disposition=PublicationDisposition.ALREADY_CURRENT,
    )


@pytest.mark.parametrize(
    ("stale_result", "current_result"),
    [
        (_result(), _findings_result()),
        (_findings_result(), _result()),
    ],
)
def test_successful_same_revision_retry_replaces_the_complete_owned_comment(
    stale_result: ReviewResult,
    current_result: ReviewResult,
) -> None:
    stale = ReviewComment(
        id=303,
        body=_expected_body(stale_result),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = ScriptedCommentGateway(comments=(stale,))

    receipt = publish_review_result(
        request=_request(),
        result=current_result,
        gateway=gateway,
    )

    assert gateway.created == []
    assert gateway.updated == [(303, _expected_body(current_result))]
    assert receipt == PublicationReceipt(
        comment_id=303,
        disposition=PublicationDisposition.UPDATED,
    )


def test_multiple_owned_revision_comments_fail_without_any_mutation() -> None:
    matches = tuple(
        ReviewComment(
            id=comment_id,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=12345),
        )
        for comment_id in (401, 402)
    )
    gateway = ScriptedCommentGateway(comments=matches)

    with pytest.raises(PublicationConsistencyError):
        publish_review_result(
            request=_request(),
            result=_result(),
            gateway=gateway,
        )

    assert gateway.created == []
    assert gateway.updated == []


@pytest.mark.parametrize(
    "spoof",
    [
        ReviewComment(
            id=501,
            body=_expected_body(),
            performed_via_github_app=None,
        ),
        ReviewComment(
            id=502,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=999),
        ),
        ReviewComment(
            id=503,
            body=f"{_expected_body()}visible text after the marker\n",
            performed_via_github_app=ReviewCommentApp(id=12345),
        ),
    ],
)
def test_marker_spoofs_do_not_redirect_publication(spoof: ReviewComment) -> None:
    gateway = ScriptedCommentGateway(comments=(spoof,))

    receipt = publish_review_result(
        request=_request(),
        result=_result(),
        gateway=gateway,
    )

    assert gateway.created == [_expected_body()]
    assert gateway.updated == []
    assert receipt.disposition is PublicationDisposition.CREATED


def test_a_new_accepted_base_revision_creates_a_distinct_comment() -> None:
    old_revision = ReviewComment(
        id=601,
        body=_expected_body(),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = ScriptedCommentGateway(comments=(old_revision,))

    receipt = publish_review_result(
        request=_request(base_sha="d" * 40),
        result=_result(),
        gateway=gateway,
    )

    assert len(gateway.created) == 1
    assert gateway.created[0] != old_revision.body
    assert "<!-- review-agent:v1:" in gateway.created[0]
    assert gateway.updated == []
    assert receipt.disposition is PublicationDisposition.CREATED


@pytest.mark.parametrize(
    "mismatched_result",
    [
        _result(repository="octo-org/other"),
        _result(pr_number=18),
        _result(
            diff_range=DiffRange(
                start_sha="c" * 40,
                end_sha="d" * 40,
            )
        ),
    ],
)
def test_mismatched_review_result_is_rejected_before_github_access(
    mismatched_result: ReviewResult,
) -> None:
    gateway = ScriptedCommentGateway()

    with pytest.raises(ValueError, match="identities do not match"):
        publish_review_result(
            request=_request(),
            result=mismatched_result,
            gateway=gateway,
        )

    assert gateway.listed == 0
    assert gateway.created == []
    assert gateway.updated == []


def test_failed_retry_preserves_the_last_successfully_published_review() -> None:
    previous = ReviewComment(
        id=701,
        body=_expected_body(),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = FailingUpdateGateway(comments=(previous,))

    with pytest.raises(RuntimeError, match="definitive update failure"):
        publish_review_result(
            request=_request(),
            result=_findings_result(),
            gateway=gateway,
        )

    assert gateway.comments == (previous,)
    assert gateway.created == []
