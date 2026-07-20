import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from review_agent import (
    DiffRange,
    ReviewRequest,
    ReviewResult,
    publish_review_result,
    render_review_comment,
)
from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.errors import FailureCategory, ReviewError
from review_agent.github import GitHubAppClient, GitHubError, GitHubMutationError


def _private_key(tmp_path: Path) -> tuple[Path, bytes]:
    key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path = tmp_path / "github-app.pem"
    path.write_bytes(private_key)
    return path, public_key


def test_installation_token_is_scoped_to_the_configured_repository(tmp_path: Path) -> None:
    private_key_path, public_key = _private_key(tmp_path)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)

    def issue_token(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/app/installations/23/access_tokens"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        authorization = request.headers["Authorization"]
        assert authorization.startswith("Bearer ")
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims == {
            "iat": int(now.timestamp()) - 60,
            "exp": int(now.timestamp()) + 600,
            "iss": "12345",
        }
        assert json.loads(request.content) == {
            "repositories": ["example"],
            "permissions": {
                "contents": "read",
                "pull_requests": "write",
            },
        }
        return httpx.Response(201, json={"token": "ghs_installation_token"})

    http_client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(issue_token),
    )
    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=http_client,
        clock=lambda: now,
    )

    token = github.installation_token(
        repository="octo-org/example",
        installation_id=23,
    )

    assert token == "ghs_installation_token"


def test_successful_result_creates_one_top_level_pull_request_comment(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)
    comment_requests: list[httpx.Request] = []
    result = ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=DiffRange(start_sha="a" * 40, end_sha="b" * 40),
        status="no_important_issues",
        findings=(),
    )
    review_request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Add feature",
    )

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        comment_requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/repos/octo-org/example/issues/17/comments"
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/example/issues/17/comments"
        assert request.headers["Authorization"] == "Bearer ghs_installation_token"
        body = json.loads(request.content)["body"]
        assert body.startswith(render_review_comment(result))
        assert "<!-- review-agent:v1:" in body
        return httpx.Response(
            201,
            json={
                "id": 101,
                "body": body,
                "performed_via_github_app": {"id": 12345},
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    publish_review_result(request=review_request, result=result, gateway=github)

    assert len(comment_requests) == 2


def test_review_comments_follow_github_continuation_links_with_maximum_page_size(
    tmp_path: Path,
) -> None:
    private_key_path, _ = _private_key(tmp_path)
    comment_requests: list[httpx.Request] = []

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        comment_requests.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer ghs_installation_token"
        if len(comment_requests) == 1:
            assert request.url.path == "/repos/octo-org/example/issues/17/comments"
            assert dict(request.url.params) == {"per_page": "100"}
            return httpx.Response(
                200,
                headers={
                    "Link": (
                        '<https://api.github.test/repos/octo-org/example/issues/17/comments'
                        '?per_page=100&page=2>; rel="next"'
                    )
                },
                json=[
                    {
                        "id": 101,
                        "body": "first",
                        "performed_via_github_app": {"id": 12345},
                    }
                ],
            )
        assert dict(request.url.params) == {"per_page": "100", "page": "2"}
        return httpx.Response(
            200,
            json=[
                {
                    "id": 102,
                    "body": "second",
                    "performed_via_github_app": None,
                },
                {
                    "id": 103,
                    "body": "foreign",
                    "performed_via_github_app": {"id": 999},
                },
            ],
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    comments = github.list_review_comments(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
    )

    assert [(comment.id, comment.body) for comment in comments] == [
        (101, "first"),
        (102, "second"),
        (103, "foreign"),
    ]
    assert comments[0].performed_via_github_app is not None
    assert comments[0].performed_via_github_app.id == 12345
    assert comments[1].performed_via_github_app is None
    assert comments[2].performed_via_github_app is not None
    assert comments[2].performed_via_github_app.id == 999


def test_review_comment_scan_fails_closed_when_the_page_bound_has_a_next_link(
    tmp_path: Path,
) -> None:
    private_key_path, _ = _private_key(tmp_path)
    comment_page_count = 0

    def github_api(request: httpx.Request) -> httpx.Response:
        nonlocal comment_page_count
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        comment_page_count += 1
        return httpx.Response(
            200,
            headers={
                "Link": (
                    '<https://api.github.test/repos/octo-org/example/issues/17/comments'
                    f'?per_page=100&page={comment_page_count + 1}>; rel="next"'
                )
            },
            json=[
                {
                    "id": comment_page_count,
                    "body": f"page {comment_page_count}",
                    "performed_via_github_app": None,
                }
            ],
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.list_review_comments(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
        )

    assert failure.value.operation == "review_comment_list"
    assert comment_page_count == 10


def test_create_review_comment_returns_the_strictly_validated_comment(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/example/issues/17/comments"
        assert json.loads(request.content) == {"body": "review body"}
        return httpx.Response(
            201,
            json={
                "id": 101,
                "body": "review body",
                "performed_via_github_app": {"id": 12345},
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    comment = github.create_review_comment(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        body="review body",
    )

    assert comment.id == 101
    assert comment.body == "review body"
    assert comment.performed_via_github_app is not None
    assert comment.performed_via_github_app.id == 12345


def test_update_review_comment_replaces_the_complete_body_and_returns_the_comment(
    tmp_path: Path,
) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        assert request.method == "PATCH"
        assert request.url.path == "/repos/octo-org/example/issues/comments/101"
        assert json.loads(request.content) == {"body": "replacement body"}
        return httpx.Response(
            200,
            json={
                "id": 101,
                "body": "replacement body",
                "performed_via_github_app": {"id": 12345},
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    comment = github.update_review_comment(
        repository="octo-org/example",
        comment_id=101,
        installation_id=23,
        body="replacement body",
    )

    assert comment.id == 101
    assert comment.body == "replacement body"


def test_comment_mutation_transport_timeout_is_classified_as_ambiguous(
    tmp_path: Path,
) -> None:
    private_key_path, _ = _private_key(tmp_path)
    leaked_material = "request_may_have_reached_github"

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        raise httpx.ReadTimeout(leaked_material, request=request)

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubMutationError) as failure:
        github.create_review_comment(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            body="review body",
        )

    assert failure.value.operation == "review_comment_create"
    assert failure.value.retry_after_seconds is None
    assert leaked_material not in str(failure.value)


@pytest.mark.parametrize(
    ("headers", "expected_delay"),
    [
        ({"Retry-After": "7"}, 7.0),
        (
            {
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1784462409",
            },
            9.0,
        ),
    ],
)
def test_retryable_comment_mutation_exposes_bounded_retry_delay(
    tmp_path: Path,
    headers: dict[str, str],
    expected_delay: float,
) -> None:
    private_key_path, _ = _private_key(tmp_path)
    now = datetime(2026, 7, 19, 12, tzinfo=UTC)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(503, headers=headers, json={"message": "untrusted"})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
        clock=lambda: now,
    )

    with pytest.raises(GitHubMutationError) as failure:
        github.update_review_comment(
            repository="octo-org/example",
            comment_id=101,
            installation_id=23,
            body="replacement body",
        )

    assert failure.value.status_code == 503
    assert failure.value.retry_after_seconds == expected_delay


def test_malformed_successful_comment_mutation_response_is_definitive(
    tmp_path: Path,
) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(
            201,
            json={
                "id": 101,
                "performed_via_github_app": {"id": 12345},
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.create_review_comment(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            body="review body",
        )

    assert not isinstance(failure.value, GitHubMutationError)
    assert failure.value.operation == "review_comment_create"


@pytest.mark.parametrize(
    "comment_document",
    [
        {
            "id": 0,
            "body": "review body",
            "performed_via_github_app": {"id": 12345},
        },
        {
            "id": 101,
            "body": None,
            "performed_via_github_app": {"id": 12345},
        },
        {
            "id": 101,
            "body": "review body",
            "performed_via_github_app": {"id": 0},
        },
        {
            "id": 101,
            "body": "review body",
        },
    ],
)
def test_review_comment_scan_rejects_malformed_comment_documents(
    tmp_path: Path,
    comment_document: dict[str, object],
) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(200, json=[comment_document])

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.list_review_comments(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
        )

    assert failure.value.operation == "review_comment_list"


def test_review_comment_scan_preserves_the_two_mib_response_limit(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": 101,
                    "body": "x" * (2 * 1024 * 1024),
                    "performed_via_github_app": None,
                }
            ],
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.list_review_comments(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
        )

    assert failure.value.operation == "review_comment_list"


def test_definitive_comment_mutation_failure_is_not_ambiguous(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(403, json={"message": "permission denied"})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.create_review_comment(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            body="review body",
        )

    assert not isinstance(failure.value, GitHubMutationError)
    assert failure.value.status_code == 403


def test_pull_request_read_returns_the_typed_immutable_request(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        assert request.method == "GET"
        assert request.url.path == "/repos/octo-org/example/pulls/17"
        assert request.headers["Authorization"] == "Bearer ghs_installation_token"
        return httpx.Response(
            200,
            json={
                "number": 17,
                "title": "Fix the parser",
                "body": None,
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    request = github.review_request(pr_number=17, installation_id=23)

    assert request.repository == "octo-org/example"
    assert request.pr_number == 17
    assert request.installation_id == 23
    assert request.base_sha == "a" * 40
    assert request.head_sha == "b" * 40
    assert request.title == "Fix the parser"
    assert request.description == ""


def test_credential_failure_is_normalized_without_secret_material(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)
    leaked_material = "ghs_must_never_escape"

    def reject_credentials(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            json={"message": f"permission denied for {leaked_material}"},
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(reject_credentials),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.installation_token(
            repository="octo-org/example",
            installation_id=23,
        )

    assert failure.value.operation == "installation_token"
    assert failure.value.status_code == 403
    assert str(failure.value) == "GitHub installation_token failed with status 403"
    assert leaked_material not in str(failure.value)


def test_github_transport_timeout_preserves_the_review_deadline_category(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def time_out(request: httpx.Request) -> httpx.Response:
        message = "untrusted response detail"
        raise httpx.ReadTimeout(message, request=request)

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(time_out),
        ),
    )

    with (
        review_deadline_scope(ReviewDeadline.after(1)),
        pytest.raises(ReviewError) as failure,
    ):
        github.installation_token(
            repository="octo-org/example",
            installation_id=23,
        )

    assert failure.value.category is FailureCategory.TIMEOUT
    assert failure.value.stage == "installation_token"
    assert "untrusted response detail" not in str(failure.value)


def test_publication_permission_failure_is_normalized_and_redacted(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)
    installation_token = "ghs_publication_secret"
    response_secret = "raw_response_must_not_escape"

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": installation_token})
        return httpx.Response(403, json={"message": response_secret})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.create_review_comment(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            body="review body",
        )

    assert failure.value.operation == "review_comment_create"
    assert failure.value.status_code == 403
    assert installation_token not in str(failure.value)
    assert response_secret not in str(failure.value)


def test_pull_request_response_failure_is_normalized(tmp_path: Path) -> None:
    private_key_path, _ = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        return httpx.Response(500, json={"message": "raw server failure"})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    with pytest.raises(GitHubError) as failure:
        github.review_request(pr_number=17, installation_id=23)

    assert failure.value.operation == "pull_request_read"
    assert failure.value.status_code == 500
    assert str(failure.value) == "GitHub pull_request_read failed with status 500"


def test_repository_installation_is_discovered_with_app_authentication(tmp_path: Path) -> None:
    private_key_path, public_key = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/octo-org/example/installation"
        authorization = request.headers["Authorization"]
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "12345"
        return httpx.Response(200, json={"id": 23})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    assert github.repository_installation_id() == 23


def test_webhook_url_is_read_with_app_authentication(tmp_path: Path) -> None:
    private_key_path, public_key = _private_key(tmp_path)

    def github_api(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/app/hook/config"
        authorization = request.headers["Authorization"]
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "12345"
        return httpx.Response(
            200,
            json={
                "url": "https://review.example.test/webhooks/github",
                "content_type": "json",
                "insecure_ssl": "0",
                "secret": "not-returned-by-client",
            },
        )

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    assert github.webhook_url() == "https://review.example.test/webhooks/github"
