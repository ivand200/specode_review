import asyncio
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from review_agent import (
    AgentReview,
    DiffRange,
    FailureCategory,
    Finding,
    Location,
    ReviewContext,
    Reviewer,
    ReviewError,
    ReviewRequest,
    ReviewResult,
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, filename: str, contents: str, message: str) -> str:
    (repository / filename).write_text(contents, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _diverged_repository(root: Path) -> tuple[Path, str, str, str]:
    repository = root / "origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    merge_base = _commit(repository, "shared.txt", "base\n", "base")

    _git(repository, "switch", "-c", "feature")
    head_sha = _commit(repository, "feature.txt", "feature\n", "feature")

    _git(repository, "switch", "main")
    base_sha = _commit(repository, "main.txt", "main\n", "main")
    return repository, merge_base, base_sha, head_sha


class CapturingRunner:
    def __init__(self) -> None:
        self.context: ReviewContext | None = None
        self.checked_out_head: str | None = None
        self.was_detached = False

    def run(self, context: ReviewContext) -> AgentReview:
        self.context = context
        self.checked_out_head = _git(context.checkout, "rev-parse", "HEAD")
        symbolic_ref = subprocess.run(
            ["git", "-C", str(context.checkout), "symbolic-ref", "-q", "HEAD"],
            check=False,
            capture_output=True,
        )
        self.was_detached = symbolic_ref.returncode != 0
        return AgentReview(findings=())


class ReturningRunner:
    def __init__(self, candidate: object) -> None:
        self.candidate = candidate
        self.workspace: Path | None = None

    def run(self, context: ReviewContext) -> AgentReview:
        self.workspace = context.workspace
        return cast("AgentReview", self.candidate)


class RaisingRunner:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.workspace: Path | None = None

    def run(self, context: ReviewContext) -> AgentReview:
        self.workspace = context.workspace
        raise self.error


class HistoryRunner:
    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    def run(self, context: ReviewContext) -> AgentReview:
        self.workspaces.append(context.workspace)
        return AgentReview(findings=())


def _finding() -> Finding:
    return Finding(
        severity="important",
        title="Feature data can be lost",
        locations=(Location(path="feature.txt", line=1),),
        evidence="The new write replaces existing data.",
        impact="A user can lose saved data.",
        suggested_fix="Preserve and merge the existing data.",
    )


def test_review_request_is_typed_and_immutable() -> None:
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Fix the parser",
    )

    assert request.repository == "octo-org/example"
    with pytest.raises(ValidationError):
        request.pr_number = 18  # type: ignore[misc]


def test_review_uses_the_exact_head_and_one_merge_base_range(tmp_path: Path) -> None:
    source, merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    runner = CapturingRunner()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=workspace_root,
        runner=runner,
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    result = reviewer.review(request)

    assert runner.context is not None
    assert runner.checked_out_head == head_sha
    assert runner.was_detached
    assert runner.context.diff_range.start_sha == merge_base
    assert runner.context.diff_range.end_sha == head_sha
    assert runner.context.manifest.diff_range is runner.context.diff_range
    assert runner.context.manifest.paths == ("feature.txt",)
    assert result.diff_range is runner.context.diff_range
    assert result.repository == request.repository
    assert result.pr_number == request.pr_number
    assert result.status == "no_important_issues"
    assert result.findings == ()
    assert not runner.context.workspace.exists()


def test_repository_failure_is_normalized_and_cleans_the_workspace(tmp_path: Path) -> None:
    source, _, base_sha, _ = _diverged_repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=workspace_root,
        runner=CapturingRunner(),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha="f" * 40,
        title="Missing head",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REPOSITORY_MATERIALIZATION
    assert failure.value.stage == "repository_materialization"
    assert list(workspace_root.iterdir()) == []


def test_invalid_runner_candidate_fails_the_review_and_cleans_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = ReturningRunner(
        {
            "findings": [
                {
                    "severity": "minor",
                    "title": "Style",
                    "locations": [{"path": "feature.txt"}],
                    "evidence": "Formatting differs",
                    "impact": "None",
                    "suggested_fix": "Reformat",
                }
            ]
        }
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert failure.value.stage == "candidate_validation"
    assert runner.workspace is not None
    assert not runner.workspace.exists()


def test_runner_timeout_is_normalized_and_cleans_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = RaisingRunner(TimeoutError())
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.TIMEOUT
    assert failure.value.stage == "review_runner"
    assert runner.workspace is not None
    assert not runner.workspace.exists()


@pytest.mark.parametrize(
    "error",
    [
        ReviewError(FailureCategory.CODEX_OR_LIMIT, stage="review_runner"),
        asyncio.CancelledError(),
        RuntimeError("unexpected"),
    ],
)
def test_runner_terminal_errors_propagate_after_cleanup(
    tmp_path: Path,
    error: BaseException,
) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = RaisingRunner(error)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    with pytest.raises(type(error)) as raised:
        reviewer.review(request)

    assert raised.value is error
    assert runner.workspace is not None
    assert not runner.workspace.exists()


def test_each_review_uses_a_unique_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = HistoryRunner()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=runner,
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    reviewer.review(request)
    reviewer.review(request)

    assert len(set(runner.workspaces)) == 2
    assert all(not workspace.exists() for workspace in runner.workspaces)


def test_typed_models_enforce_identity_and_result_bounds() -> None:
    finding = _finding()
    diff_range = DiffRange(start_sha="a" * 40, end_sha="b" * 40)

    with pytest.raises(ValidationError):
        ReviewRequest(
            repository="octo-org/example",
            pr_number=0,
            installation_id=23,
            base_sha="a" * 40,
            head_sha="b" * 40,
            title="Invalid PR",
        )
    with pytest.raises(ValidationError):
        DiffRange(start_sha="not-a-sha", end_sha="b" * 40)
    with pytest.raises(ValidationError):
        Location(path="feature.txt", line=0)
    with pytest.raises(ValidationError):
        Finding(
            severity="important",
            title="x" * 161,
            locations=(Location(path="feature.txt"),),
            evidence="Evidence",
            impact="Impact",
            suggested_fix="Fix",
        )
    with pytest.raises(ValidationError):
        AgentReview(findings=(finding,) * 6)
    with pytest.raises(ValidationError):
        ReviewResult(
            repository="octo-org/example",
            pr_number=17,
            diff_range=diff_range,
            status="no_important_issues",
            findings=(finding,),
        )
    with pytest.raises(ValidationError):
        finding.title = "Mutated"  # type: ignore[misc]
