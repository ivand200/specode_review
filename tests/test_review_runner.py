import pytest

from review_agent import PreflightOutcome, ReviewRunner
from review_agent.errors import FailureCategory, ReviewError
from review_agent.github import (
    GitHubError,
    GitHubOperation,
    ReviewComment,
    ReviewCommentApp,
)
from review_agent.models import ReviewRequest


def _request(**updates: object) -> ReviewRequest:
    return ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Add feature",
    ).model_copy(update=updates)


class ControlledPreflightClient:
    def __init__(
        self,
        *,
        comments: tuple[ReviewComment, ...] = (),
        failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.closed = False
        self.requests: list[tuple[str, int, int]] = []
        self.comments = comments
        self.failure = failure
        self.close_failure = close_failure

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
        self.requests.append((repository, pr_number, installation_id))
        if self.failure is not None:
            raise self.failure
        return self.comments

    def close(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


def test_authorized_repository_revision_is_ready_and_closes_call_local_client() -> None:
    client = ControlledPreflightClient()
    repositories: list[str] = []

    def create_client(repository: str) -> ControlledPreflightClient:
        repositories.append(repository)
        return client

    runner = ReviewRunner(github_client_factory=create_client)

    assert runner.preflight(_request()) is PreflightOutcome.READY
    assert repositories == ["octo-org/example"]
    assert client.requests == [("octo-org/example", 17, 23)]
    assert client.closed


def test_each_repository_and_installation_uses_a_fresh_scoped_client() -> None:
    clients = [ControlledPreflightClient(), ControlledPreflightClient()]
    repositories: list[str] = []

    def create_client(repository: str) -> ControlledPreflightClient:
        repositories.append(repository)
        return clients[len(repositories) - 1]

    runner = ReviewRunner(github_client_factory=create_client)

    first = runner.preflight(_request())
    second = runner.preflight(
        _request(repository="Other-Org/Second", installation_id=91, pr_number=8)
    )

    assert (first, second) == (PreflightOutcome.READY, PreflightOutcome.READY)
    assert repositories == ["octo-org/example", "other-org/second"]
    assert clients[0].requests == [("octo-org/example", 17, 23)]
    assert clients[1].requests == [("other-org/second", 8, 91)]
    assert all(client.closed for client in clients)


def test_exact_specode_review_app_comment_is_already_reviewed() -> None:
    marker = (
        "<!-- specode-review:v1:"
        "b3fdc634e74cf30721e4dc24158636348334fa1c133b44a74eb401e89db2119f -->"
    )
    client = ControlledPreflightClient(
        comments=(
            ReviewComment(
                id=71,
                body=(
                    "historical legacy review\n\n"
                    f"{marker.replace('specode-review', 'review-agent')}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
            ReviewComment(
                id=72,
                body=f"foreign review\n\n{marker}\n",
                performed_via_github_app=ReviewCommentApp(id=54321),
            ),
            ReviewComment(
                id=73,
                body=f"SpeCodeReview result\n\n{marker}\n",
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        )
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    assert runner.preflight(_request()) is PreflightOutcome.ALREADY_REVIEWED
    assert client.closed


def test_repository_installation_mismatch_is_not_authorized_and_closes_client() -> None:
    client = ControlledPreflightClient(
        failure=GitHubError(
            GitHubOperation.INSTALLATION_TOKEN,
            status_code=404,
        )
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    assert runner.preflight(_request()) is PreflightOutcome.NOT_AUTHORIZED
    assert client.closed


def test_ambiguous_authentication_failure_is_normalized_and_closes_client() -> None:
    client = ControlledPreflightClient(
        failure=GitHubError(
            GitHubOperation.INSTALLATION_TOKEN,
            status_code=403,
        )
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    with pytest.raises(ReviewError) as failure:
        runner.preflight(_request())

    assert failure.value.category is FailureCategory.REVIEW_FAILURE
    assert failure.value.stage == "preflight"
    assert str(failure.value) == "review_failure during preflight"
    assert client.closed


def test_timeout_failure_is_normalized_and_closes_client() -> None:
    client = ControlledPreflightClient(
        failure=ReviewError(FailureCategory.TIMEOUT, stage="review_comment_list")
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    with pytest.raises(ReviewError) as failure:
        runner.preflight(_request())

    assert (failure.value.category, failure.value.stage) == (
        FailureCategory.REVIEW_FAILURE,
        "preflight",
    )
    assert client.closed


def test_unexpected_provider_failure_exposes_only_the_safe_preflight_failure() -> None:
    client = ControlledPreflightClient(
        failure=RuntimeError("provider response included ghs_sensitive_token")
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    with pytest.raises(ReviewError) as failure:
        runner.preflight(_request())

    assert str(failure.value) == "review_failure during preflight"
    assert failure.value.__cause__ is None
    assert "ghs_sensitive_token" not in repr(failure.value)
    assert client.closed


def test_client_close_failure_suppresses_ready_outcome_and_is_normalized() -> None:
    client = ControlledPreflightClient(
        close_failure=RuntimeError("close leaked ghs_sensitive_token")
    )
    runner = ReviewRunner(github_client_factory=lambda _repository: client)

    with pytest.raises(ReviewError) as failure:
        runner.preflight(_request())

    assert str(failure.value) == "review_failure during preflight"
    assert failure.value.__cause__ is None
    assert client.closed


def test_client_acquisition_failure_is_normalized() -> None:
    def fail_to_create_client(_repository: str) -> ControlledPreflightClient:
        message = "client construction exposed ghs_sensitive_token"
        raise RuntimeError(message)

    runner = ReviewRunner(github_client_factory=fail_to_create_client)

    with pytest.raises(ReviewError) as failure:
        runner.preflight(_request())

    assert str(failure.value) == "review_failure during preflight"
    assert failure.value.__cause__ is None
