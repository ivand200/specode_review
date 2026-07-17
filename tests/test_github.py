import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from review_agent import DiffRange, ReviewResult, publish_review_result, render_review_comment
from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.errors import FailureCategory, ReviewError
from review_agent.github import GitHubAppClient, GitHubError


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
            "permissions": {"contents": "read", "pull_requests": "write"},
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

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        comment_requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/example/issues/17/comments"
        assert request.headers["Authorization"] == "Bearer ghs_installation_token"
        assert json.loads(request.content) == {"body": render_review_comment(result)}
        return httpx.Response(201, json={"id": 101})

    github = GitHubAppClient(
        repository="octo-org/example",
        app_id=12345,
        private_key_path=private_key_path,
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github_api),
        ),
    )

    publish_review_result(result, github, installation_id=23)

    assert len(comment_requests) == 1


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
        github.publish(
            repository="octo-org/example",
            pr_number=17,
            installation_id=23,
            body="review body",
        )

    assert failure.value.operation == "publication"
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
