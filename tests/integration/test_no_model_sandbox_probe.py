import os
import subprocess
from pathlib import Path

import pytest

from review_agent import (
    CandidateAcceptance,
    FailureCategory,
    Reviewer,
    ReviewError,
    ReviewRequest,
)
from review_agent.configuration import SandboxOperationPolicy
from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.resources import AttemptResources, ReviewResourceManager

from .no_model_sandbox_probe import NoModelDockerSandboxProbe

pytestmark = [
    pytest.mark.docker_sandbox,
    pytest.mark.skipif(
        os.environ.get("RUN_DOCKER_SANDBOX_E2E") != "1",
        reason="set RUN_DOCKER_SANDBOX_E2E=1 to use the Docker Sandbox runtime",
    ),
]


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Sandbox Test")
    _git(repository, "config", "user.email", "sandbox@example.com")
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    _git(repository, "commit", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repository, "add", "feature.txt")
    _git(repository, "commit", "-m", "feature")
    return repository, base_sha, _git(repository, "rev-parse", "HEAD")


def _request(*, base_sha: str, head_sha: str, title: str) -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/sandbox-fixture",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title=title,
    )


def _reviewer(
    *,
    source: Path,
    resources: AttemptResources,
    probe: NoModelDockerSandboxProbe,
    timeout: bool = False,
) -> Reviewer:
    return Reviewer(
        repository="octo-org/sandbox-fixture",
        source_repository=source,
        resources=resources,
        candidate_acceptance=CandidateAcceptance(
            adapter=probe.candidate_adapter(resources=resources, timeout=timeout),
            max_bytes=65_536,
        ),
    )


def test_test_only_no_model_probe_is_fresh_bounded_and_swept(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    probe = NoModelDockerSandboxProbe(
        config=SandboxOperationPolicy(
            process_output_max_bytes=65_536,
            cleanup_timeout_seconds=30,
        )
    )
    prefix = "review-agent-it-"
    resource_manager = ReviewResourceManager(
        workspace_root=workspace_root,
        sandbox_prefix=prefix,
        sandbox_client=probe,
    )
    resources = resource_manager.for_attempt("a" * 32)
    reviewer = _reviewer(
        source=source,
        resources=resources,
        probe=probe,
    )
    request = _request(base_sha=base_sha, head_sha=head_sha, title="Test-only no-model probe")
    cleanup_names: list[str] = [resources.sandbox_name]

    try:
        first = reviewer.review(request)

        assert first.status == "no_important_issues"
        assert resources.sandbox_name not in probe.list_names()
        assert _git(source, "status", "--short") == ""
        assert _git(source, "rev-parse", "HEAD") == head_sha
        assert list(workspace_root.iterdir()) == []

        fresh_resources = resource_manager.for_attempt("d" * 32)
        cleanup_names.append(fresh_resources.sandbox_name)
        fresh_reviewer = _reviewer(
            source=source,
            resources=fresh_resources,
            probe=probe,
        )

        assert fresh_reviewer.review(request).status == "no_important_issues"
        assert fresh_resources.sandbox_name not in probe.list_names()

        timeout_resources = resource_manager.for_attempt("b" * 32)
        cleanup_names.append(timeout_resources.sandbox_name)
        timeout_reviewer = _reviewer(
            source=source,
            resources=timeout_resources,
            probe=probe,
            timeout=True,
        )
        with (
            review_deadline_scope(ReviewDeadline.after(1)),
            pytest.raises(ReviewError) as timeout_failure,
        ):
            timeout_reviewer.review(request)
        assert timeout_failure.value.category is FailureCategory.TIMEOUT
        assert timeout_resources.sandbox_name not in probe.list_names()
        assert list(workspace_root.iterdir()) == []

        orphan_resources = resource_manager.for_attempt("c" * 32)
        cleanup_names.append(orphan_resources.sandbox_name)
        orphan_workspace = orphan_resources.workspace
        orphan_workspace.mkdir()
        orphan_control = tmp_path / "orphan-control"
        orphan_control.mkdir()
        orphan_name = orphan_resources.sandbox_name
        probe.create_stale(
            resources=orphan_resources,
            control=orphan_control,
            checkout=source,
        )
        unrelated_manager = ReviewResourceManager(
            workspace_root=workspace_root,
            sandbox_prefix="unrelated-it-",
            sandbox_client=probe,
        )
        unrelated_resources = unrelated_manager.for_attempt("e" * 32)
        cleanup_names.append(unrelated_resources.sandbox_name)
        unrelated_control = tmp_path / "unrelated-control"
        unrelated_control.mkdir()
        probe.create_stale(
            resources=unrelated_resources,
            control=unrelated_control,
            checkout=source,
        )

        resource_manager.sweep_stale()

        assert not orphan_workspace.exists()
        visible_names = probe.list_names()
        assert orphan_name not in visible_names
        assert unrelated_resources.sandbox_name in visible_names
        assert not any(name.startswith(prefix) for name in visible_names)
    finally:
        probe.cleanup(cleanup_names)
        probe.cleanup(cleanup_names)
