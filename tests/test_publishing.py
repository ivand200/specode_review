from collections.abc import Callable

import pytest

from specode_review.accepted_revision import AcceptedRevision
from specode_review.github import (
    GitHubError,
    GitHubMutationError,
    GitHubOperation,
    ReviewComment,
    ReviewCommentApp,
)
from specode_review.models import DiffRange, Finding, Location, ReviewRequest, ReviewResult
from specode_review.publishing import (
    PUBLICATION_RECHECK_DELAYS_SECONDS,
    PublicationConsistencyError,
    PublicationDisposition,
    PublicationReceipt,
    PublicationUnknownError,
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


class AmbiguousCreateGateway(ScriptedCommentGateway):
    def __init__(self, *, reconciled_comment: Callable[[], ReviewComment]) -> None:
        super().__init__()
        self._reconciled_comment = reconciled_comment

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        comments = super().list_review_comments(
            repository=repository,
            pr_number=pr_number,
            installation_id=installation_id,
        )
        if self.listed > 1:
            return (self._reconciled_comment(),)
        return comments

    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        del repository, pr_number, installation_id
        self.created.append(body)
        raise GitHubMutationError(GitHubOperation.REVIEW_COMMENT_CREATE)


class AmbiguousUpdateGateway(ScriptedCommentGateway):
    def __init__(self, *, stale: ReviewComment, reconciled: ReviewComment) -> None:
        super().__init__(comments=(stale,))
        self._reconciled = reconciled

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        comments = super().list_review_comments(
            repository=repository,
            pr_number=pr_number,
            installation_id=installation_id,
        )
        return (self._reconciled,) if self.listed > 1 else comments

    def update_review_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        del repository, installation_id
        self.updated.append((comment_id, body))
        raise GitHubMutationError(GitHubOperation.REVIEW_COMMENT_UPDATE)


class UnconfirmedCreateGateway(ScriptedCommentGateway):
    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        del repository, pr_number, installation_id
        self.created.append(body)
        raise GitHubMutationError(GitHubOperation.REVIEW_COMMENT_CREATE)


class DuplicateAfterCreateGateway(UnconfirmedCreateGateway):
    def __init__(self, *, duplicates: tuple[ReviewComment, ...]) -> None:
        super().__init__()
        self._duplicates = duplicates

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        comments = super().list_review_comments(
            repository=repository,
            pr_number=pr_number,
            installation_id=installation_id,
        )
        return self._duplicates if self.listed > 1 else comments


class IncompleteRecheckGateway(UnconfirmedCreateGateway):
    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        comments = super().list_review_comments(
            repository=repository,
            pr_number=pr_number,
            installation_id=installation_id,
        )
        if self.listed > 1:
            raise GitHubError(GitHubOperation.REVIEW_COMMENT_LIST)
        return comments


class RateLimitedCreateGateway(AmbiguousCreateGateway):
    def create_review_comment(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> ReviewComment:
        del repository, pr_number, installation_id
        self.created.append(body)
        raise GitHubMutationError(
            GitHubOperation.REVIEW_COMMENT_CREATE,
            status_code=429,
            retry_after_seconds=7.0,
        )


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
    marker = f"<!-- {AcceptedRevision.from_review_request(_request()).external_id} -->"
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


def test_ambiguous_create_is_reconciled_when_a_recheck_confirms_the_comment() -> None:
    gateway = AmbiguousCreateGateway(
        reconciled_comment=lambda: ReviewComment(
            id=102,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=12345),
        )
    )

    receipt = publish_review_result(
        request=_request(),
        result=_result(),
        gateway=gateway,
        sleeper=lambda _delay: None,
    )

    assert gateway.listed == 2
    assert gateway.created == [_expected_body()]
    assert gateway.updated == []
    assert receipt == PublicationReceipt(
        comment_id=102,
        disposition=PublicationDisposition.RECONCILED,
    )


def test_ambiguous_update_is_reconciled_without_issuing_another_mutation() -> None:
    stale = ReviewComment(
        id=103,
        body=_expected_body(),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = AmbiguousUpdateGateway(
        stale=stale,
        reconciled=stale.model_copy(update={"body": _expected_body(_findings_result())}),
    )

    receipt = publish_review_result(
        request=_request(),
        result=_findings_result(),
        gateway=gateway,
        sleeper=lambda _delay: None,
    )

    assert gateway.listed == 2
    assert gateway.created == []
    assert gateway.updated == [(103, _expected_body(_findings_result()))]
    assert receipt == PublicationReceipt(
        comment_id=103,
        disposition=PublicationDisposition.RECONCILED,
    )


def test_ambiguous_create_with_no_match_stops_after_fixed_rechecks() -> None:
    gateway = UnconfirmedCreateGateway()
    sleeps: list[float] = []

    with pytest.raises(PublicationUnknownError):
        publish_review_result(
            request=_request(),
            result=_result(),
            gateway=gateway,
            sleeper=sleeps.append,
        )

    assert gateway.listed == 1 + len(PUBLICATION_RECHECK_DELAYS_SECONDS)
    assert len(gateway.created) == 1
    assert gateway.updated == []
    assert sleeps == [delay for delay in PUBLICATION_RECHECK_DELAYS_SECONDS if delay]


def test_ambiguous_update_with_only_the_stale_body_remains_unknown() -> None:
    stale = ReviewComment(
        id=104,
        body=_expected_body(),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )
    gateway = AmbiguousUpdateGateway(stale=stale, reconciled=stale)

    with pytest.raises(PublicationUnknownError):
        publish_review_result(
            request=_request(),
            result=_findings_result(),
            gateway=gateway,
            sleeper=lambda _delay: None,
        )

    assert gateway.listed == 1 + len(PUBLICATION_RECHECK_DELAYS_SECONDS)
    assert gateway.created == []
    assert gateway.updated == [(104, _expected_body(_findings_result()))]


def test_ambiguous_mutation_recheck_with_multiple_matches_is_inconsistent() -> None:
    duplicates = tuple(
        ReviewComment(
            id=comment_id,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=12345),
        )
        for comment_id in (105, 106)
    )
    gateway = DuplicateAfterCreateGateway(duplicates=duplicates)

    with pytest.raises(PublicationConsistencyError):
        publish_review_result(
            request=_request(),
            result=_result(),
            gateway=gateway,
            sleeper=lambda _delay: None,
        )

    assert gateway.listed == 2
    assert len(gateway.created) == 1
    assert gateway.updated == []


def test_ambiguous_mutation_with_an_incomplete_recheck_remains_unknown() -> None:
    gateway = IncompleteRecheckGateway()

    with pytest.raises(PublicationUnknownError):
        publish_review_result(
            request=_request(),
            result=_result(),
            gateway=gateway,
            sleeper=lambda _delay: None,
        )

    assert gateway.listed == 2
    assert len(gateway.created) == 1
    assert gateway.updated == []


def test_rate_limit_delay_is_honored_when_it_fits_the_review_deadline() -> None:
    gateway = RateLimitedCreateGateway(
        reconciled_comment=lambda: ReviewComment(
            id=107,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=12345),
        )
    )
    sleeps: list[float] = []

    receipt = publish_review_result(
        request=_request(),
        result=_result(),
        gateway=gateway,
        sleeper=sleeps.append,
        remaining_time=lambda: 8.0,
    )

    assert sleeps == [7.0]
    assert gateway.listed == 2
    assert receipt == PublicationReceipt(
        comment_id=107,
        disposition=PublicationDisposition.RECONCILED,
    )


def test_rate_limit_delay_that_cannot_fit_leaves_publication_unknown() -> None:
    gateway = RateLimitedCreateGateway(
        reconciled_comment=lambda: ReviewComment(
            id=108,
            body=_expected_body(),
            performed_via_github_app=ReviewCommentApp(id=12345),
        )
    )
    sleeps: list[float] = []

    with pytest.raises(PublicationUnknownError):
        publish_review_result(
            request=_request(),
            result=_result(),
            gateway=gateway,
            sleeper=sleeps.append,
            remaining_time=lambda: 7.0,
        )

    assert sleeps == []
    assert gateway.listed == 1
    assert len(gateway.created) == 1
    assert gateway.updated == []


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
    assert "<!-- specode-review:v1:" in gateway.created[0]
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
