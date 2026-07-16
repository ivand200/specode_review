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
    publish_review_result,
    render_review_comment,
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


def _grounding_repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    _commit(repository, "shared.txt", "unchanged\n", "shared")
    base_sha = _commit(repository, "deleted.txt", "removed later\n", "deleted base")

    _git(repository, "switch", "-c", "feature")
    (repository / "deleted.txt").unlink()
    (repository / "feature.txt").write_text("first\nsecond\n", encoding="utf-8")
    (repository / "binary.bin").write_bytes(b"text\x00binary")
    outside = root / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repository / "escape-link").symlink_to(outside)
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "grounding cases")
    head_sha = _git(repository, "rev-parse", "HEAD")
    return repository, base_sha, head_sha


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


class CapturingPublisher:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    def publish(self, *, repository: str, pr_number: int, body: str) -> None:
        self.comments.append((repository, pr_number, body))


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


def test_grounding_rejects_a_traversal_location(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    finding = _finding().model_copy(
        update={"locations": (Location(path="../outside.txt"),)},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=ReturningRunner(AgentReview(findings=(finding,))),
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
    assert failure.value.stage == "candidate_grounding"


def test_grounding_accepts_head_files_and_deleted_changed_paths(tmp_path: Path) -> None:
    source, base_sha, head_sha = _grounding_repository(tmp_path)
    finding = _finding().model_copy(
        update={
            "locations": (
                Location(path="feature.txt", line=2),
                Location(path="deleted.txt", description="Deleted by this change"),
            )
        },
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=ReturningRunner(AgentReview(findings=(finding,))),
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

    assert result.status == "issues_found"
    assert result.findings == (finding,)
    assert result.repository == request.repository
    assert result.pr_number == request.pr_number
    assert result.diff_range.end_sha == request.head_sha


def test_review_preserves_the_fake_runners_finding_order(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    first = _finding().model_copy(update={"title": "First by material impact"})
    second = _finding().model_copy(
        update={"severity": "blocking", "title": "Second by material impact"},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=ReturningRunner(AgentReview(findings=(first, second))),
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

    assert result.findings == (first, second)


@pytest.mark.parametrize(
    "location",
    [
        Location(path="/feature.txt"),
        Location(path="missing.txt"),
        Location(path="shared.txt"),
        Location(path="feature.txt", line=3),
        Location(path="binary.bin", line=1),
        Location(path="deleted.txt", line=1),
        Location(path="escape-link"),
    ],
    ids=[
        "absolute",
        "nonexistent",
        "no-changed-path",
        "line-out-of-range",
        "binary-line",
        "deleted-line",
        "symlink-escape",
    ],
)
def test_grounding_rejects_ungrounded_locations(
    tmp_path: Path,
    location: Location,
) -> None:
    source, base_sha, head_sha = _grounding_repository(tmp_path)
    finding = _finding().model_copy(update={"locations": (location,)})
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=ReturningRunner(AgentReview(findings=(finding,))),
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
    assert failure.value.stage == "candidate_grounding"


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


def test_finding_models_enforce_all_declared_string_and_collection_bounds() -> None:
    location = Location(path="feature.txt")
    valid_finding = {
        "severity": "important",
        "title": "Title",
        "locations": (location,),
        "evidence": "Evidence",
        "impact": "Impact",
        "suggested_fix": "Fix",
    }

    with pytest.raises(ValidationError):
        Location(path="x" * 513)
    with pytest.raises(ValidationError):
        Location(path="feature.txt", description="x" * 241)
    with pytest.raises(ValidationError):
        Finding.model_validate({**valid_finding, "locations": (location,) * 4})
    with pytest.raises(ValidationError):
        Finding.model_validate({**valid_finding, "evidence": "x" * 1_201})
    with pytest.raises(ValidationError):
        Finding.model_validate({**valid_finding, "impact": "x" * 601})
    with pytest.raises(ValidationError):
        Finding.model_validate({**valid_finding, "suggested_fix": "x" * 601})


def test_render_review_comment_is_deterministic_and_contains_the_typed_result() -> None:
    result = ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=DiffRange(start_sha="a" * 40, end_sha="b" * 40),
        status="issues_found",
        findings=(_finding(),),
    )

    comment = render_review_comment(result)

    assert comment == render_review_comment(result)
    assert "Automated code review" in comment
    assert f"{result.diff_range.start_sha}..{result.diff_range.end_sha}" in comment
    assert "Issues found" in comment
    assert "important" in comment
    assert "Feature data can be lost" in comment
    assert "feature.txt:1" in comment
    assert "The new write replaces existing data." in comment
    assert "A user can lose saved data." in comment
    assert "Preserve and merge the existing data." in comment


def test_render_review_comment_neutralizes_model_authored_markdown() -> None:
    payload = "@octocat <b>hidden</b> <!-- marker -->\n# Application heading"
    finding = _finding().model_copy(
        update={
            "title": payload,
            "locations": (Location(path="feature.txt", description=payload),),
            "evidence": payload,
            "impact": payload,
            "suggested_fix": payload,
        }
    )
    result = ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=DiffRange(start_sha="a" * 40, end_sha="b" * 40),
        status="issues_found",
        findings=(finding,),
    )

    comment = render_review_comment(result)

    assert "\n# Application heading" not in comment
    assert "@octocat <b>hidden</b> <!-- marker --> # Application heading" in comment
    assert all(
        line.startswith(("# Automated code review", "## Issues found", "### Finding"))
        for line in comment.splitlines()
        if line.startswith("#")
    )


@pytest.mark.parametrize("fill", ["`", "😀"])
def test_every_maximum_sized_review_renders_below_githubs_comment_limit(fill: str) -> None:
    finding = Finding(
        severity="important",
        title=fill * 160,
        locations=tuple(Location(path=fill * 512, description=fill * 240) for _ in range(3)),
        evidence=fill * 1_200,
        impact=fill * 600,
        suggested_fix=fill * 600,
    )
    result = ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=DiffRange(start_sha="a" * 40, end_sha="b" * 40),
        status="issues_found",
        findings=(finding,) * 5,
    )

    comment = render_review_comment(result)

    assert len(comment.encode("utf-8")) < 65_536


def test_publish_review_result_creates_exactly_one_comment_for_each_success() -> None:
    publisher = CapturingPublisher()
    diff_range = DiffRange(start_sha="a" * 40, end_sha="b" * 40)
    clean_result = ReviewResult(
        repository="octo-org/example",
        pr_number=17,
        diff_range=diff_range,
        status="no_important_issues",
        findings=(),
    )
    findings_result = clean_result.model_copy(
        update={"status": "issues_found", "findings": (_finding(),)},
    )

    publish_review_result(clean_result, publisher)
    assert publisher.comments == [("octo-org/example", 17, render_review_comment(clean_result))]

    publisher.comments.clear()
    publish_review_result(findings_result, publisher)
    assert publisher.comments == [("octo-org/example", 17, render_review_comment(findings_result))]


def test_a_failed_review_produces_no_publishable_comment(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        workspace_root=tmp_path / "workspaces",
        runner=ReturningRunner(
            AgentReview(
                findings=(
                    _finding().model_copy(
                        update={"locations": (Location(path="not-real.txt"),)},
                    ),
                )
            )
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Add feature",
    )

    with pytest.raises(ReviewError):
        reviewer.review(request)

    assert publisher.comments == []
