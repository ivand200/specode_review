import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from review_agent.errors import ReviewError
from review_agent.github import (
    CHECK_RUN_NAME,
    GITHUB_RESPONSE_MAX_BYTES,
    CheckRunConclusion,
    CheckRunOutputKind,
    CheckRunStatus,
    GitHubAppClient,
    GitHubError,
    ReviewIdentity,
    derive_review_identity,
    render_check_run_presentation,
)
from review_agent.models import ReviewRequest


def _private_key(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "github-app.pem"
    path.write_bytes(private_key)
    return path


def _request(**overrides: object) -> ReviewRequest:
    values: dict[str, object] = {
        "repository": "Octo-Org/Example",
        "pr_number": 17,
        "installation_id": 23,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "title": "Fix the parser",
        "description": "",
    }
    values.update(overrides)
    return ReviewRequest.model_validate(values)


def _identity() -> ReviewIdentity:
    return derive_review_identity(_request())


def _github(
    tmp_path: Path,
    transport: httpx.BaseTransport,
    *,
    repository: str = "Octo-Org/Example",
) -> GitHubAppClient:
    return GitHubAppClient(
        repository=repository,
        app_id=12345,
        private_key_path=_private_key(tmp_path),
        http_client=httpx.Client(
            base_url="https://api.github.test",
            transport=transport,
        ),
    )


def _token_or(request: httpx.Request, response: httpx.Response) -> httpx.Response:
    if request.url.path == "/app/installations/23/access_tokens":
        return httpx.Response(201, json={"token": "ghs_installation_token"})
    return response


def _check_run_document(**overrides: object) -> dict[str, object]:
    identity = _identity()
    values: dict[str, object] = {
        "id": 101,
        "name": CHECK_RUN_NAME,
        "head_sha": identity.head_sha,
        "external_id": identity.external_id,
        "status": "completed",
        "conclusion": "neutral",
        "app": {"id": 12345},
        "output": {"title": "Review incomplete", "summary": "Retry is available."},
        "actions": [
            {
                "label": "Retry review",
                "description": "Retry this incomplete advisory review.",
                "identifier": "retry_review",
            }
        ],
    }
    values.update(overrides)
    return values


def test_review_identity_is_versioned_normalized_and_revision_bound() -> None:
    identity = derive_review_identity(_request())
    same_identity = derive_review_identity(_request(repository="octo-org/example"))

    assert identity.repository == "octo-org/example"
    assert identity.external_id.startswith("review-agent:v1:")
    assert identity.external_id == same_identity.external_id
    assert identity.external_id != derive_review_identity(
        _request(base_sha="c" * 40)
    ).external_id
    assert identity.external_id != derive_review_identity(
        _request(head_sha="d" * 40)
    ).external_id
    assert identity.external_id != derive_review_identity(_request(pr_number=18)).external_id
    assert identity.external_id != derive_review_identity(
        _request(repository="octo-org/another")
    ).external_id
    assert "Fix the parser" not in identity.external_id


def test_review_identity_rejects_invalid_external_id() -> None:
    identity = _identity()

    with pytest.raises(ValidationError):
        ReviewIdentity(
            repository=identity.repository,
            pr_number=identity.pr_number,
            base_sha=identity.base_sha,
            head_sha=identity.head_sha,
            external_id="not-versioned",
        )


def test_list_check_runs_uses_bounded_filters_and_parses_requested_actions(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/repos/Octo-Org/Example/commits/{'b' * 40}/check-runs"
        assert dict(request.url.params) == {
            "check_name": CHECK_RUN_NAME,
            "app_id": "12345",
            "filter": "all",
            "per_page": "100",
            "page": "1",
        }
        return httpx.Response(
            200,
            json={"total_count": 1, "check_runs": [_check_run_document()]},
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    check_runs = github.list_check_runs(identity=_identity(), installation_id=23)

    assert len(requests) == 1
    assert check_runs[0].actions[0].identifier == "retry_review"
    assert github.is_owned_check_run(check_runs[0], identity=_identity()) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Other Check"),
        ("head_sha", "c" * 40),
        ("external_id", "review-agent:v1:" + "0" * 64),
        ("app", {"id": 999}),
    ],
)
def test_owned_check_run_requires_every_application_identity_field(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    document = _check_run_document(**{field: value})

    def github_api(request: httpx.Request) -> httpx.Response:
        return _token_or(
            request,
            httpx.Response(200, json={"total_count": 1, "check_runs": [document]}),
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))
    check_run = github.list_check_runs(identity=_identity(), installation_id=23)[0]

    assert github.is_owned_check_run(check_run, identity=_identity()) is False


def test_owned_check_run_requires_the_configured_repository(tmp_path: Path) -> None:
    def github_api(request: httpx.Request) -> httpx.Response:
        return _token_or(
            request,
            httpx.Response(
                200,
                json={"total_count": 1, "check_runs": [_check_run_document()]},
            ),
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))
    check_run = github.list_check_runs(identity=_identity(), installation_id=23)[0]
    other_repository = derive_review_identity(_request(repository="octo-org/another"))

    assert github.is_owned_check_run(check_run, identity=other_repository) is False


def test_presentations_are_advisory_and_retry_only_incomplete_attempts() -> None:
    clean = render_check_run_presentation(
        CheckRunOutputKind.CLEAN,
        identity=_identity(),
        finding_count=0,
    )
    findings = render_check_run_presentation(
        CheckRunOutputKind.FINDINGS,
        identity=_identity(),
        finding_count=2,
    )
    incomplete = render_check_run_presentation(
        CheckRunOutputKind.TECHNICAL_FAILURE,
        identity=_identity(),
        failure_stage="publication",
        failure_category="external_service",
    )

    assert (clean.status, clean.conclusion, clean.actions) == (
        CheckRunStatus.COMPLETED,
        CheckRunConclusion.SUCCESS,
        (),
    )
    assert (findings.status, findings.conclusion, findings.actions) == (
        CheckRunStatus.COMPLETED,
        CheckRunConclusion.NEUTRAL,
        (),
    )
    assert incomplete.status is CheckRunStatus.COMPLETED
    assert incomplete.conclusion is CheckRunConclusion.NEUTRAL
    assert [action.identifier for action in incomplete.actions] == ["retry_review"]
    assert "publication" in incomplete.output.summary
    assert "external_service" in incomplete.output.summary


@pytest.mark.parametrize(
    "output_kind",
    [CheckRunOutputKind.TIMEOUT, CheckRunOutputKind.PUBLICATION_UNKNOWN],
)
def test_timeout_and_unknown_publication_are_incomplete_and_retryable(
    output_kind: CheckRunOutputKind,
) -> None:
    presentation = render_check_run_presentation(output_kind, identity=_identity())

    assert presentation.status is CheckRunStatus.COMPLETED
    assert presentation.conclusion is CheckRunConclusion.NEUTRAL
    assert [action.identifier for action in presentation.actions] == ["retry_review"]
    if output_kind is CheckRunOutputKind.PUBLICATION_UNKNOWN:
        assert "Retrying may duplicate a previously published comment." in (
            presentation.output.summary
        )


def test_technical_failure_rejects_untrusted_output_details() -> None:
    with pytest.raises(ValueError, match="failure_stage"):
        render_check_run_presentation(
            CheckRunOutputKind.TECHNICAL_FAILURE,
            identity=_identity(),
            failure_stage="publication: leaked response",
            failure_category="review_failure",
        )


def test_queued_and_running_presentations_are_not_completed() -> None:
    queued = render_check_run_presentation(CheckRunOutputKind.QUEUED, identity=_identity())
    running = render_check_run_presentation(CheckRunOutputKind.RUNNING, identity=_identity())

    assert (queued.status, queued.conclusion, queued.actions) == (
        CheckRunStatus.QUEUED,
        None,
        (),
    )
    assert (running.status, running.conclusion, running.actions) == (
        CheckRunStatus.IN_PROGRESS,
        None,
        (),
    )


def test_create_get_and_update_check_run_use_strict_payloads(tmp_path: Path) -> None:
    identity = _identity()
    requested_paths: list[str] = []

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        requested_paths.append(request.url.path)
        if request.method == "POST":
            assert json.loads(request.content) == {
                "name": CHECK_RUN_NAME,
                "head_sha": identity.head_sha,
                "external_id": identity.external_id,
                "status": "queued",
                "output": {
                    "title": "Review queued",
                    "summary": (
                        "Review Agent queued an advisory review for accepted range "
                        f"{'a' * 12}..{'b' * 12}."
                    ),
                },
            }
            return httpx.Response(
                201,
                json=_check_run_document(status="queued", conclusion=None, actions=[]),
            )
        if request.method == "GET":
            return httpx.Response(200, json=_check_run_document())
        assert request.method == "PATCH"
        assert json.loads(request.content) == {
            "status": "completed",
            "conclusion": "success",
            "output": {
                "title": "Review complete — no important findings",
                "summary": (
                    "Review Agent completed the advisory review for accepted range "
                    f"{'a' * 12}..{'b' * 12} with no important findings."
                ),
            },
            "actions": [],
        }
        return httpx.Response(
            200,
            json=_check_run_document(conclusion="success", actions=[]),
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    created = github.create_check_run(identity=identity, installation_id=23)
    fetched = github.get_check_run(check_run_id=101, installation_id=23)
    updated = github.update_check_run(
        check_run_id=101,
        installation_id=23,
        presentation=render_check_run_presentation(
            CheckRunOutputKind.CLEAN,
            identity=identity,
            finding_count=0,
        ),
    )

    assert created.id == fetched.id == updated.id == 101
    assert requested_paths == [
        "/repos/Octo-Org/Example/check-runs",
        "/repos/Octo-Org/Example/check-runs/101",
        "/repos/Octo-Org/Example/check-runs/101",
    ]


def test_malformed_check_run_response_is_normalized_without_body(tmp_path: Path) -> None:
    response_secret = "raw_response_must_not_escape"

    def github_api(request: httpx.Request) -> httpx.Response:
        return _token_or(
            request,
            httpx.Response(200, json={"id": response_secret}),
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(GitHubError) as failure:
        github.get_check_run(check_run_id=101, installation_id=23)

    assert failure.value.operation == "check_run_read"
    assert response_secret not in str(failure.value)


def test_check_run_unexpected_status_is_normalized_and_redacted(tmp_path: Path) -> None:
    installation_token = "ghs_check_run_secret"
    response_secret = "raw_response_must_not_escape"

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": installation_token})
        return httpx.Response(502, json={"message": response_secret})

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(GitHubError) as failure:
        github.get_check_run(check_run_id=101, installation_id=23)

    assert failure.value.operation == "check_run_read"
    assert failure.value.status_code == 502
    assert installation_token not in str(failure.value)
    assert response_secret not in str(failure.value)


def test_check_run_timeout_uses_the_normalized_operation(tmp_path: Path) -> None:
    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        timeout_detail = "untrusted timeout detail"
        raise httpx.ReadTimeout(timeout_detail, request=request)

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(ReviewError) as failure:
        github.get_check_run(check_run_id=101, installation_id=23)

    assert failure.value.stage == "check_run_read"
    assert "untrusted timeout detail" not in str(failure.value)


def test_oversized_check_run_response_is_rejected_before_json_validation(
    tmp_path: Path,
) -> None:
    def github_api(request: httpx.Request) -> httpx.Response:
        return _token_or(
            request,
            httpx.Response(200, content=b"x" * (GITHUB_RESPONSE_MAX_BYTES + 1)),
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(GitHubError):
        github.get_check_run(check_run_id=101, installation_id=23)


@pytest.mark.parametrize(
    "document",
    [
        _check_run_document(id="101"),
        _check_run_document(status="waiting"),
        _check_run_document(actions=[{"identifier": "retry_review"}]),
        _check_run_document(conclusion="success"),
    ],
)
def test_check_run_response_rejects_malformed_trusted_fields(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    def github_api(request: httpx.Request) -> httpx.Response:
        return _token_or(request, httpx.Response(200, json=document))

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(GitHubError):
        github.get_check_run(check_run_id=101, installation_id=23)


def test_list_check_runs_rejects_results_beyond_pagination_limit(tmp_path: Path) -> None:
    pages: list[int] = []

    def github_api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/23/access_tokens":
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "total_count": 1_001,
                "check_runs": [
                    _check_run_document(id=page * 100 + offset) for offset in range(100)
                ],
            },
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    with pytest.raises(GitHubError) as failure:
        github.list_check_runs(identity=_identity(), installation_id=23)

    assert failure.value.operation == "check_run_list"
    assert pages == list(range(1, 11))


def test_list_check_runs_reads_multiple_pages_with_one_installation_token(
    tmp_path: Path,
) -> None:
    token_requests = 0
    pages: list[int] = []

    def github_api(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path == "/app/installations/23/access_tokens":
            token_requests += 1
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        page = int(request.url.params["page"])
        pages.append(page)
        count = 100 if page == 1 else 1
        return httpx.Response(
            200,
            json={
                "total_count": 101,
                "check_runs": [
                    _check_run_document(id=(page - 1) * 100 + offset + 1)
                    for offset in range(count)
                ],
            },
        )

    github = _github(tmp_path, httpx.MockTransport(github_api))

    check_runs = github.list_check_runs(identity=_identity(), installation_id=23)

    assert len(check_runs) == 101
    assert token_requests == 1
    assert pages == [1, 2]
