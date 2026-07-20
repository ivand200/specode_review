from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Never

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
from review_agent.live import LiveProfilePreconditionError
from review_agent.models import ReviewRequest
from tests.live import test_full_live, test_github_live


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/test-example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Polluted live fixture",
        description="",
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


class PollutedGitHub:
    check_runs: tuple[CheckRun, ...] = ()
    comments: tuple[ReviewComment, ...] = ()

    def __init__(self, **kwargs: object) -> None:
        del kwargs

    @property
    def app_id(self) -> int:
        return 12345

    def repository_installation_id(self) -> int:
        return 23

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
        assert (pr_number, installation_id) == (17, 23)
        return _request()

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        assert identity == derive_review_identity(_request())
        assert installation_id == 23
        return self.check_runs

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
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
        assert (repository, pr_number, installation_id) == (
            "octo-org/test-example",
            17,
            23,
        )
        return self.comments


def _unexpected_effect(*args: object, **kwargs: object) -> Never:
    del args, kwargs
    pytest.fail("polluted live fixture reached an external effect")


def test_checkpoint_b_rejects_owned_check_run_before_local_or_service_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = derive_review_identity(_request())
    PollutedGitHub.check_runs = (_owned_check_run(identity),)
    PollutedGitHub.comments = ()
    resources_path = tmp_path / "resources.jsonl"
    monkeypatch.setenv("RUN_LIVE_GITHUB_E2E", "1")
    monkeypatch.setenv("E2E_GITHUB_REPOSITORY", "octo-org/test-example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/test-example")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("E2E_CREATED_RESOURCES_PATH", str(resources_path))
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(tmp_path / "key.pem"))
    monkeypatch.setenv("E2E_GITHUB_PR_NUMBER", "17")
    monkeypatch.setenv("E2E_EXPECTED_BASE_SHA", _request().base_sha)
    monkeypatch.setenv("E2E_EXPECTED_HEAD_SHA", _request().head_sha)
    monkeypatch.setattr(test_github_live, "GitHubAppClient", PollutedGitHub)
    monkeypatch.setattr(test_github_live, "_ControlledLauncher", _unexpected_effect)
    monkeypatch.setattr(test_github_live, "_serve", _unexpected_effect)
    monkeypatch.setattr(test_github_live, "_record_resources", _unexpected_effect)

    with pytest.raises(LiveProfilePreconditionError):
        test_github_live.test_real_retry_exercises_the_exact_revision_comment_lifecycle(tmp_path)

    assert list(tmp_path.iterdir()) == []
    assert not resources_path.exists()


def test_checkpoint_c_rejects_owned_comment_before_sandbox_model_or_service_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    marker = f"<!-- {derive_review_identity(request).external_id} -->"
    PollutedGitHub.check_runs = ()
    PollutedGitHub.comments = (
        ReviewComment(
            id=72,
            body=f"historical review\n\n{marker}\n",
            performed_via_github_app=ReviewCommentApp(id=12345),
        ),
    )
    workspace_root = tmp_path / "workspace"
    resources_path = tmp_path / "resources.jsonl"
    settings = SimpleNamespace(
        webhook=SimpleNamespace(repository=request.repository, secret="secret"),
        attempt=SimpleNamespace(
            app_id=12345,
            private_key_path=tmp_path / "key.pem",
            workspace_root=workspace_root,
        ),
    )
    monkeypatch.setenv("RUN_FULL_LIVE_E2E", "1")
    monkeypatch.setenv("ACKNOWLEDGE_MODEL_COST", "1")
    monkeypatch.setenv("E2E_GITHUB_REPOSITORY", request.repository)
    monkeypatch.setenv("E2E_GITHUB_PR_NUMBER", "17")
    monkeypatch.setenv("E2E_EXPECTED_BASE_SHA", request.base_sha)
    monkeypatch.setenv("E2E_EXPECTED_HEAD_SHA", request.head_sha)
    monkeypatch.setenv("E2E_EXPECTED_FINDING", "seeded defect")
    monkeypatch.setenv("E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT", "hostile instruction")
    monkeypatch.setenv("E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT", "hostile config")
    monkeypatch.setenv("E2E_CREATED_RESOURCES_PATH", str(resources_path))
    monkeypatch.setattr(
        test_full_live.ProductionSettings,
        "from_environment",
        lambda _environment: settings,
    )
    monkeypatch.setattr(test_full_live, "GitHubAppClient", PollutedGitHub)
    monkeypatch.setattr(test_full_live, "DockerSandboxClient", _unexpected_effect)
    monkeypatch.setattr(test_full_live, "create_production_app", _unexpected_effect)
    monkeypatch.setattr(test_full_live, "_serve", _unexpected_effect)
    monkeypatch.setattr(test_full_live, "_record_resources", _unexpected_effect)

    with pytest.raises(LiveProfilePreconditionError):
        test_full_live.test_full_live_production_lifecycle_reviews_and_publishes()

    assert not workspace_root.exists()
    assert not resources_path.exists()


def test_checkpoint_c_shutdown_failure_prevents_resource_record_append(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    PollutedGitHub.check_runs = ()
    PollutedGitHub.comments = ()
    workspace_root = tmp_path / "workspace"
    resources_path = tmp_path / "resources.jsonl"
    settings = SimpleNamespace(
        webhook=SimpleNamespace(repository=request.repository, secret="secret"),
        attempt=SimpleNamespace(
            app_id=12345,
            private_key_path=tmp_path / "key.pem",
            workspace_root=workspace_root,
            process_output_max_bytes=1_048_576,
            review_timeout_seconds=30,
            sandbox_cleanup_timeout_seconds=5,
            sandbox_name_prefix="review-agent-",
        ),
    )

    class Sandbox:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

    @contextmanager
    def failed_shutdown(_app: object) -> Iterator[str]:
        yield "http://checkpoint-c.test"
        message = "simulated graceful shutdown failure"
        raise RuntimeError(message)

    monkeypatch.setenv("RUN_FULL_LIVE_E2E", "1")
    monkeypatch.setenv("ACKNOWLEDGE_MODEL_COST", "1")
    monkeypatch.setenv("E2E_GITHUB_REPOSITORY", request.repository)
    monkeypatch.setenv("E2E_GITHUB_PR_NUMBER", "17")
    monkeypatch.setenv("E2E_EXPECTED_BASE_SHA", request.base_sha)
    monkeypatch.setenv("E2E_EXPECTED_HEAD_SHA", request.head_sha)
    monkeypatch.setenv("E2E_EXPECTED_FINDING", "seeded defect")
    monkeypatch.setenv("E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT", "hostile instruction")
    monkeypatch.setenv("E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT", "hostile config")
    monkeypatch.setenv("E2E_CREATED_RESOURCES_PATH", str(resources_path))
    monkeypatch.setattr(
        test_full_live.ProductionSettings,
        "from_environment",
        lambda _environment: settings,
    )
    monkeypatch.setattr(test_full_live, "GitHubAppClient", PollutedGitHub)
    monkeypatch.setattr(test_full_live, "DockerSandboxClient", Sandbox)
    monkeypatch.setattr(test_full_live, "create_production_app", lambda **_kwargs: object())
    monkeypatch.setattr(test_full_live, "_serve", failed_shutdown)
    monkeypatch.setattr(
        test_full_live,
        "_send_signed_webhook",
        lambda *_args, **_kwargs: (202, '{"status":"accepted"}'),
    )
    monkeypatch.setattr(
        test_full_live,
        "_wait_for_check_run",
        lambda *_args, **_kwargs: _owned_check_run(derive_review_identity(request)),
    )
    monkeypatch.setattr(test_full_live, "_finish_checkpoint_c", _unexpected_effect)

    with pytest.raises(RuntimeError, match="simulated graceful shutdown failure"):
        test_full_live.test_full_live_production_lifecycle_reviews_and_publishes()

    assert not resources_path.exists()
