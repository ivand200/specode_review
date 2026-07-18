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
    SandboxResourceLimits,
)
from review_agent.configuration import SandboxOperationPolicy
from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.resources import ReviewResourceManager
from review_agent.sandbox import (
    DockerSandboxClient,
    SandboxLifecycleAdapter,
)

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


class RecordingDockerSandboxClient(DockerSandboxClient):
    def __init__(self) -> None:
        super().__init__(
            config=SandboxOperationPolicy(
                process_output_max_bytes=65_536,
                cleanup_timeout_seconds=30,
            )
        )
        self.created_names: list[str] = []
        self.removed_names: list[str] = []

    def create(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        resources: SandboxResourceLimits,
    ) -> None:
        self.created_names.append(name)
        super().create(
            name=name,
            control=control,
            checkout=checkout,
            resources=resources,
        )

    def remove(self, name: str) -> None:
        self.removed_names.append(name)
        super().remove(name)


def _request(*, base_sha: str, head_sha: str, title: str) -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/sandbox-fixture",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title=title,
    )


def test_no_model_sandbox_lifecycle_is_fresh_bounded_and_swept(tmp_path: Path) -> None:
    source, base_sha, head_sha = _repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    client = RecordingDockerSandboxClient()
    prefix = "review-agent-it-"
    resource_manager = ReviewResourceManager(
        workspace_root=workspace_root,
        sandbox_prefix=prefix,
        sandbox_client=client,
    )
    resources = resource_manager.for_attempt("a" * 32)
    probe_command = (
        "sh",
        "-c",
        "set -eu; "
        "command -v curl >/dev/null; "
        "if curl -fsS --max-time 3 https://example.com >/dev/null 2>&1; then exit 74; fi; "
        "test ! -e /home/agent/review-attempt-marker; "
        "touch /home/agent/review-attempt-marker; "
        "rm feature.txt; "
        "printf '{\"findings\":[]}'",
    )
    runner = SandboxLifecycleAdapter(
        client=client,
        resources=resources,
        review_command=probe_command,
    )
    reviewer = Reviewer(
        repository="octo-org/sandbox-fixture",
        source_repository=source,
        resources=resources,
        candidate_acceptance=CandidateAcceptance(adapter=runner, max_bytes=65_536),
    )
    request = _request(base_sha=base_sha, head_sha=head_sha, title="No-model lifecycle")

    try:
        first = reviewer.review(request)

        assert first.status == "no_important_issues"
        assert client.created_names == [resources.sandbox_name]
        assert client.removed_names == client.created_names
        assert _git(source, "status", "--short") == ""
        assert _git(source, "rev-parse", "HEAD") == head_sha
        assert list(workspace_root.iterdir()) == []

        timeout_resources = resource_manager.for_attempt("b" * 32)
        timeout_runner = SandboxLifecycleAdapter(
            client=client,
            resources=timeout_resources,
            review_command=("sleep", "30"),
        )
        timeout_reviewer = Reviewer(
            repository="octo-org/sandbox-fixture",
            source_repository=source,
            resources=timeout_resources,
            candidate_acceptance=CandidateAcceptance(
                adapter=timeout_runner,
                max_bytes=65_536,
            ),
        )
        with (
            review_deadline_scope(ReviewDeadline.after(1)),
            pytest.raises(ReviewError) as timeout_failure,
        ):
            timeout_reviewer.review(request)
        assert timeout_failure.value.category is FailureCategory.TIMEOUT
        assert list(workspace_root.iterdir()) == []

        orphan_resources = resource_manager.for_attempt("c" * 32)
        orphan_workspace = orphan_resources.workspace
        orphan_workspace.mkdir()
        orphan_control = tmp_path / "orphan-control"
        orphan_control.mkdir()
        orphan_name = orphan_resources.sandbox_name
        client.create(
            name=orphan_name,
            control=orphan_control,
            checkout=source,
            resources=SandboxResourceLimits(),
        )

        resource_manager.sweep_stale()

        assert not orphan_workspace.exists()
        assert orphan_name in client.removed_names
        assert not any(name.startswith(prefix) for name in client.list_names())
    finally:
        for name in client.list_names():
            if name.startswith(prefix):
                client.remove(name)
