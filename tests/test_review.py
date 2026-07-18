import asyncio
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from review_agent import (
    AgentReview,
    CandidateAcceptance,
    DiffRange,
    FailureCategory,
    Finding,
    GitHubRepository,
    Location,
    ReviewContext,
    Reviewer,
    ReviewError,
    ReviewLimits,
    ReviewRequest,
    ReviewResult,
    SandboxResourceLimits,
    publish_review_result,
    render_review_comment,
)
from review_agent.configuration import CANDIDATE_OUTPUT_MAX_BYTES
from review_agent.core import CandidateContract
from review_agent.resources import AttemptResources, ReviewResourceManager
from review_agent.sandbox import SandboxLifecycleAdapter


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


def _changed_repository(
    root: Path,
    changes: dict[str, bytes],
    *,
    base_files: dict[str, bytes] | None = None,
) -> tuple[Path, str, str]:
    repository = root / "sized-origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    for filename, contents in (base_files or {}).items():
        path = repository / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    _git(repository, "add", "--all")
    _git(repository, "commit", "--allow-empty", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    for filename, contents in changes.items():
        path = repository / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "sized change")
    head_sha = _git(repository, "rev-parse", "HEAD")
    return repository, base_sha, head_sha


def _github_pull_ref_repository(root: Path) -> tuple[Path, str, str, str, str]:
    repository = root / "github-origin"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    merge_base = _commit(repository, "shared.txt", "base\n", "base")

    _git(repository, "switch", "-c", "fork-feature")
    accepted_head = _commit(repository, "feature.txt", "accepted\n", "accepted head")
    newer_head = _commit(repository, "feature.txt", "newer\n", "newer head")
    _git(repository, "update-ref", "refs/pull/17/head", newer_head)

    _git(repository, "switch", "main")
    base_sha = _commit(repository, "main.txt", "main\n", "accepted base")
    _git(repository, "branch", "-D", "fork-feature")
    return repository, merge_base, base_sha, accepted_head, newer_head


def _acceptance(
    adapter: object,
    *,
    max_bytes: int = CANDIDATE_OUTPUT_MAX_BYTES,
) -> CandidateAcceptance:
    return CandidateAcceptance(adapter=adapter, max_bytes=max_bytes)  # type: ignore[arg-type]


def _resources(
    workspace_root: Path,
    *,
    sandbox_prefix: str = "review-agent-",
) -> AttemptResources:
    return AttemptResources.for_attempt(
        "a" * 32,
        workspace_root=workspace_root,
        sandbox_prefix=sandbox_prefix,
    )


class CapturingAdapter:
    def __init__(self) -> None:
        self.context: ReviewContext | None = None
        self.checked_out_head: str | None = None
        self.was_detached = False

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.context = context
        self.checked_out_head = _git(context.checkout, "rev-parse", "HEAD")
        symbolic_ref = subprocess.run(
            ["git", "-C", str(context.checkout), "symbolic-ref", "-q", "HEAD"],
            check=False,
            capture_output=True,
        )
        self.was_detached = symbolic_ref.returncode != 0
        return b'{"findings":[]}'


class ReturningAdapter:
    def __init__(self, candidate: object) -> None:
        self.candidate = candidate
        self.workspace: Path | None = None

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.workspace = context.workspace
        return self.candidate  # type: ignore[return-value]


class RaisingAdapter:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.workspace: Path | None = None

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.workspace = context.workspace
        raise self.error


class HistoryAdapter:
    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.workspaces.append(context.workspace)
        return b'{"findings":[]}'


class RecordingSandboxClient:
    def __init__(  # noqa: PLR0913 - test boundary controls independent failure modes.
        self,
        *,
        head_sha: str,
        existing_names: tuple[str, ...] = (),
        command_outputs: dict[tuple[str, ...], bytes] | None = None,
        command_errors: dict[tuple[str, ...], BaseException] | None = None,
        create_error: Exception | None = None,
        remove_error: Exception | None = None,
    ) -> None:
        self.head_sha = head_sha
        self.existing_names = existing_names
        self.command_outputs = command_outputs or {}
        self.command_errors = command_errors or {}
        self.create_error = create_error
        self.remove_error = remove_error
        self.created: list[tuple[str, Path, Path, SandboxResourceLimits]] = []
        self.executed: list[tuple[str, tuple[str, ...], str | None, int]] = []
        self.removed: list[str] = []

    def create(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        resources: SandboxResourceLimits,
    ) -> None:
        self.created.append((name, control, checkout, resources))
        if self.create_error is not None:
            raise self.create_error

    def execute(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        workdir: str | None,
        process_limit: int,
    ) -> bytes:
        self.executed.append((name, command, workdir, process_limit))
        if error := self.command_errors.get(command):
            raise error
        if command == ("git", "rev-parse", "HEAD"):
            return f"{self.head_sha}\n".encode()
        return self.command_outputs.get(command, b"")

    def remove(self, name: str) -> None:
        self.removed.append(name)
        if self.remove_error is not None:
            raise self.remove_error

    def list_names(self) -> tuple[str, ...]:
        return self.existing_names


class GitHubCapturingAdapter:
    def __init__(self) -> None:
        self.context: ReviewContext | None = None
        self.checked_out_head: str | None = None
        self.remote_url: str | None = None

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del contract
        self.context = context
        self.checked_out_head = _git(context.checkout, "rev-parse", "HEAD")
        self.remote_url = _git(context.checkout, "remote", "get-url", "origin")
        return AgentReview(findings=(_finding(),)).model_dump_json().encode()


class FakeInstallationCredentials:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int]] = []

    def installation_token(self, *, repository: str, installation_id: int) -> str:
        self.requests.append((repository, installation_id))
        return "ghs_test_installation_token"


class CapturingPublisher:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    def publish(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
        body: str,
    ) -> None:
        del installation_id
        self.comments.append((repository, pr_number, body))


def _finding() -> Finding:
    return Finding(
        severity="important",
        title="Feature data can be lost",
        locations=(Location(path="feature.txt", line=1, description=None),),
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


def test_review_request_counts_unicode_text_by_character_and_truncates_description() -> None:
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="😀" * 256,
        description="😀" * 10_001,
    )

    assert len(request.title) == 256
    assert len(request.title.encode("utf-8")) == 1_024
    assert len(request.description) == 10_000
    assert request.description.endswith("\n\n[truncated]")
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate(
            {
                **request.model_dump(),
                "title": "😀" * 257,
            }
        )


def test_review_uses_the_exact_head_and_one_merge_base_range(tmp_path: Path) -> None:
    source, merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    runner = CapturingAdapter()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(runner),
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


def test_review_creates_exact_writable_sandbox_copy_and_removes_it(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    client = RecordingSandboxClient(head_sha=head_sha)
    owned_resources = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="review-agent-",
        sandbox_client=client,
    ).for_attempt("a" * 32)
    runner = SandboxLifecycleAdapter(client=client, resources=owned_resources)
    sandbox_resources = SandboxResourceLimits(cpus=3, memory_mib=2_048, pids=64)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=owned_resources,
        candidate_acceptance=_acceptance(runner),
        limits=ReviewLimits(sandbox_resources=sandbox_resources),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Isolate the review",
    )

    result = reviewer.review(request)

    assert result.status == "no_important_issues"
    assert len(client.created) == 1
    sandbox_name, control, checkout, applied_resources = client.created[0]
    assert sandbox_name == "review-agent-" + "a" * 32
    assert control.parent == owned_resources.workspace
    assert control.name == "control"
    assert checkout.name == "checkout"
    assert applied_resources is sandbox_resources
    assert client.executed == [
        (
            sandbox_name,
            (
                "sh",
                "-c",
                'if touch "$1/.review-agent-write-probe"; then exit 73; fi',
                "review-agent-read-only-check",
                str(checkout),
            ),
            None,
            64,
        ),
        (
            sandbox_name,
            ("mkdir", "-p", "/home/agent/review/repo"),
            None,
            64,
        ),
        (
            sandbox_name,
            ("cp", "-R", f"{checkout}/.", "/home/agent/review/repo"),
            None,
            64,
        ),
        (
            sandbox_name,
            ("git", "rev-parse", "HEAD"),
            "/home/agent/review/repo",
            64,
        ),
    ]
    assert client.removed == [sandbox_name]


def test_review_rejects_an_inexact_sandbox_copy_after_forced_removal(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    client = RecordingSandboxClient(head_sha="f" * 40)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(client=client, resources=_resources(tmp_path / "workspaces"))
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Reject an inexact sandbox copy",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.SANDBOX_LIFECYCLE
    assert failure.value.stage == "sandbox_head_verification"
    assert len(client.removed) == 1
    assert client.removed[0] == client.created[0][0]


def test_sandbox_adapter_construction_does_not_sweep_owned_names(tmp_path: Path) -> None:
    owned = "review-agent-" + "a" * 32
    client = RecordingSandboxClient(
        head_sha="f" * 40,
        existing_names=(
            owned,
            "review-agent-not-a-uuid",
            "other-" + "b" * 32,
        ),
    )
    runner = SandboxLifecycleAdapter(client=client, resources=_resources(tmp_path / "workspaces"))

    Reviewer(
        repository="octo-org/example",
        source_repository=tmp_path / "unused-origin",
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
    )

    assert client.removed == []


def test_reviewer_construction_does_not_sweep_owned_workspaces(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    owned = workspace_root / ("review-agent-workspace-" + "a" * 32)
    similarly_named = workspace_root / "review-agent-workspace-not-a-uuid"
    unrelated = workspace_root / ("other-" + "b" * 32)
    owned.mkdir()
    similarly_named.mkdir()
    unrelated.mkdir()

    Reviewer(
        repository="octo-org/example",
        source_repository=tmp_path / "unused-origin",
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(CapturingAdapter()),
    )

    assert owned.exists()
    assert similarly_named.exists()
    assert unrelated.exists()


def test_review_returns_only_the_candidate_from_the_vm_local_copy(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    review_command = ("sh", "-c", "rm feature.txt; printf '{\"findings\":[]}'")
    client = RecordingSandboxClient(
        head_sha=head_sha,
        command_outputs={review_command: b'{"findings":[]}'},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(
                client=client,
                resources=_resources(tmp_path / "workspaces"),
                review_command=review_command,
            ),
            max_bytes=len(b'{"findings":[]}'),
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Run a no-model sandbox probe",
    )

    result = reviewer.review(request)

    assert result.status == "no_important_issues"
    assert client.executed[-1][1] == review_command
    assert client.executed[-1][2] == "/home/agent/review/repo"
    assert _git(source, "show", f"{head_sha}:feature.txt") == "feature"
    assert _git(source, "status", "--short") == ""
    assert len(client.removed) == 1


def test_review_fails_when_forced_sandbox_removal_fails(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    client = RecordingSandboxClient(
        head_sha=head_sha,
        remove_error=RuntimeError("untrusted cleanup detail"),
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(client=client, resources=_resources(tmp_path / "workspaces"))
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Require sandbox cleanup",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.SANDBOX_LIFECYCLE
    assert failure.value.stage == "sandbox_cleanup"


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_category"),
    [
        (RuntimeError("command failed"), ReviewError, FailureCategory.SANDBOX_LIFECYCLE),
        (TimeoutError(), ReviewError, FailureCategory.TIMEOUT),
        (asyncio.CancelledError(), asyncio.CancelledError, None),
    ],
)
def test_sandbox_command_terminal_failures_force_removal(
    tmp_path: Path,
    error: BaseException,
    expected_type: type[BaseException],
    expected_category: FailureCategory | None,
) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    review_command = ("probe",)
    client = RecordingSandboxClient(
        head_sha=head_sha,
        command_errors={review_command: error},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(
                client=client,
                resources=_resources(tmp_path / "workspaces"),
                review_command=review_command,
            )
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Exercise terminal sandbox failure",
    )

    with pytest.raises(expected_type) as failure:
        reviewer.review(request)

    if expected_category is not None:
        assert isinstance(failure.value, ReviewError)
        assert failure.value.category is expected_category
    assert len(client.removed) == 1


def test_invalid_sandbox_candidate_is_rejected_after_forced_removal(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    review_command = ("probe",)
    client = RecordingSandboxClient(
        head_sha=head_sha,
        command_outputs={review_command: b"not-json"},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(
                client=client,
                resources=_resources(tmp_path / "workspaces"),
                review_command=review_command,
            )
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Reject invalid sandbox output",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert client.removed == [client.created[0][0]]


def test_sandbox_lifecycle_adapter_detects_candidate_overflow_and_cleans_up(
    tmp_path: Path,
) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    review_command = ("probe",)
    client = RecordingSandboxClient(
        head_sha=head_sha,
        command_outputs={review_command: b'{"findings":[]}'},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(
                client=client,
                resources=_resources(tmp_path / "workspaces"),
                review_command=review_command,
            ),
            max_bytes=len(b'{"findings":[]}') - 1,
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Bound lifecycle candidate bytes",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.CODEX_OR_LIMIT
    assert failure.value.stage == "sandbox_candidate_output"
    assert client.removed == [client.created[0][0]]


def test_sandbox_setup_failure_still_attempts_forced_removal(tmp_path: Path) -> None:
    source, _merge_base, base_sha, head_sha = _diverged_repository(tmp_path)
    client = RecordingSandboxClient(
        head_sha=head_sha,
        create_error=RuntimeError("setup failed"),
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            SandboxLifecycleAdapter(client=client, resources=_resources(tmp_path / "workspaces"))
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Fail sandbox setup safely",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.SANDBOX_LIFECYCLE
    assert client.removed == [client.created[0][0]]


def test_review_rejects_more_than_one_hundred_changed_files_before_the_runner(
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha = _changed_repository(
        tmp_path,
        {f"changed-{index:03}.txt": b"changed\n" for index in range(101)},
    )
    runner = CapturingAdapter()
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(runner),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Large change",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REVIEW_TOO_LARGE
    assert failure.value.stage == "review_size"
    assert runner.context is None
    assert list(workspace_root.iterdir()) == []


def test_review_accepts_exact_file_and_text_line_limits_without_counting_binary_lines(
    tmp_path: Path,
) -> None:
    changes = {f"binary-{index:03}.bin": b"content\x00binary\n" for index in range(99)}
    changes["text.txt"] = b"new line\n" * 2_500
    source, base_sha, head_sha = _changed_repository(
        tmp_path,
        changes,
        base_files={"text.txt": b"old line\n" * 2_500},
    )
    runner = CapturingAdapter()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Boundary-sized change",
    )

    result = reviewer.review(request)

    assert result.status == "no_important_issues"
    assert runner.context is not None
    assert runner.context.manifest.changed_files == 100
    assert runner.context.manifest.changed_text_lines == 5_000
    assert runner.context.manifest.paths[-1] == "text.txt"


def test_review_rejects_more_than_five_thousand_changed_text_lines_before_the_runner(
    tmp_path: Path,
) -> None:
    source, base_sha, head_sha = _changed_repository(
        tmp_path,
        {"text.txt": b"line\n" * 5_001},
    )
    runner = CapturingAdapter()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Line-heavy change",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REVIEW_TOO_LARGE
    assert failure.value.stage == "review_size"
    assert runner.context is None


def test_github_materialization_uses_pull_ref_but_reviews_the_accepted_head(
    tmp_path: Path,
) -> None:
    source, merge_base, base_sha, accepted_head, newer_head = _github_pull_ref_repository(tmp_path)
    credentials = FakeInstallationCredentials()
    runner = GitHubCapturingAdapter()
    reviewer = Reviewer(
        repository="octo-org/example",
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
        source_repository=GitHubRepository(
            credentials=credentials,
            clone_url=str(source),
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=accepted_head,
        title="Fork contribution",
    )

    result = reviewer.review(request)
    comment = render_review_comment(result)

    assert credentials.requests == [("octo-org/example", 23)]
    assert runner.context is not None
    assert runner.checked_out_head == accepted_head
    assert runner.checked_out_head != newer_head
    assert runner.context.diff_range.start_sha == merge_base
    assert runner.context.diff_range.end_sha == accepted_head
    assert runner.context.manifest.diff_range is runner.context.diff_range
    assert runner.context.manifest.paths == ("feature.txt",)
    assert result.diff_range is runner.context.diff_range
    assert result.findings == (_finding(),)
    assert f"{merge_base}..{accepted_head}" in comment
    assert runner.remote_url == str(source)
    assert "ghs_test_installation_token" not in runner.remote_url


def test_github_materialization_failure_redacts_credentials_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    credentials = FakeInstallationCredentials()
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(CapturingAdapter()),
        source_repository=GitHubRepository(
            credentials=credentials,
            clone_url=str(tmp_path / "missing-origin"),
        ),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Unavailable repository",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REPOSITORY_MATERIALIZATION
    assert "ghs_test_installation_token" not in str(failure.value)
    assert "ghs_test_installation_token" not in repr(failure.value.__cause__)
    assert list(workspace_root.iterdir()) == []


def test_repository_failure_is_normalized_and_cleans_the_workspace(tmp_path: Path) -> None:
    source, _, base_sha, _ = _diverged_repository(tmp_path)
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(CapturingAdapter()),
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


def test_review_bounds_captured_git_process_output(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = CapturingAdapter()
    workspace_root = tmp_path / "workspaces"
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(workspace_root),
        candidate_acceptance=_acceptance(runner),
        limits=ReviewLimits(process_output_max_bytes=1),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Bound process diagnostics",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REPOSITORY_MATERIALIZATION
    assert failure.value.stage == "repository_materialization"
    assert runner.context is None
    assert list(workspace_root.iterdir()) == []


def test_review_carries_validated_sandbox_resource_limits_to_the_runner(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = CapturingAdapter()
    sandbox_resources = SandboxResourceLimits(cpus=2, memory_mib=4_096, pids=256)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
        limits=ReviewLimits(sandbox_resources=sandbox_resources),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Bound sandbox resources",
    )

    reviewer.review(request)

    assert runner.context is not None
    assert runner.context.sandbox_resources is sandbox_resources
    with pytest.raises(ValueError, match="sandbox resource limits must be positive"):
        SandboxResourceLimits(cpus=0, memory_mib=4_096, pids=256)


def test_review_fails_when_successful_workspace_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(ReturningAdapter(b'{"findings":[]}')),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Require workspace cleanup",
    )

    cleanup_error = "untrusted cleanup detail"

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError(cleanup_error)

    monkeypatch.setattr("review_agent.core.shutil.rmtree", fail_cleanup)

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.REVIEW_FAILURE
    assert failure.value.stage == "workspace_cleanup"
    assert "untrusted cleanup detail" not in str(failure.value)


def test_invalid_runner_candidate_fails_the_review_and_cleans_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = ReturningAdapter(
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
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
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


def test_review_accepts_a_candidate_at_the_exact_byte_limit(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    candidate = b'{"findings":[]}'
    candidate += b" " * (65_536 - len(candidate))
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(ReturningAdapter(candidate)),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Bound candidate bytes",
    )

    result = reviewer.review(request)

    assert result.status == "no_important_issues"


def test_review_rejects_a_candidate_immediately_beyond_the_byte_limit(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    candidate = b'{"findings":[]}'
    candidate += b" " * (65_537 - len(candidate))
    runner = ReturningAdapter(candidate)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
    )
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=base_sha,
        head_sha=head_sha,
        title="Reject oversized candidate bytes",
    )

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.CODEX_OR_LIMIT
    assert failure.value.stage == "candidate_output"
    assert runner.workspace is not None
    assert not runner.workspace.exists()


def test_grounding_rejects_a_traversal_location(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    finding = _finding().model_copy(
        update={"locations": (Location(path="../outside.txt", line=None, description=None),)},
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            ReturningAdapter(AgentReview(findings=(finding,)).model_dump_json().encode())
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

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert failure.value.stage == "candidate_grounding"


def test_grounding_accepts_head_files_and_deleted_changed_paths(tmp_path: Path) -> None:
    source, base_sha, head_sha = _grounding_repository(tmp_path)
    finding = _finding().model_copy(
        update={
            "locations": (
                Location(path="feature.txt", line=2, description=None),
                Location(
                    path="deleted.txt",
                    line=None,
                    description="Deleted by this change",
                ),
            )
        },
    )
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            ReturningAdapter(AgentReview(findings=(finding,)).model_dump_json().encode())
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
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            ReturningAdapter(AgentReview(findings=(first, second)).model_dump_json().encode())
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

    result = reviewer.review(request)

    assert result.findings == (first, second)


@pytest.mark.parametrize(
    "location",
    [
        Location(path="/feature.txt", line=None, description=None),
        Location(path="missing.txt", line=None, description=None),
        Location(path="shared.txt", line=None, description=None),
        Location(path="feature.txt", line=3, description=None),
        Location(path="binary.bin", line=1, description=None),
        Location(path="deleted.txt", line=1, description=None),
        Location(path="escape-link", line=None, description=None),
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
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            ReturningAdapter(AgentReview(findings=(finding,)).model_dump_json().encode())
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

    with pytest.raises(ReviewError) as failure:
        reviewer.review(request)

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert failure.value.stage == "candidate_grounding"


def test_runner_timeout_is_normalized_and_cleans_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = RaisingAdapter(TimeoutError())
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
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
    runner = RaisingAdapter(error)
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
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


def test_reviewer_uses_only_its_assigned_attempt_workspace(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    runner = HistoryAdapter()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(runner),
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

    assert set(runner.workspaces) == {
        tmp_path / "workspaces" / ("review-agent-workspace-" + "a" * 32)
    }
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
        Location(path="feature.txt", line=0, description=None)
    with pytest.raises(ValidationError):
        Finding(
            severity="important",
            title="x" * 161,
            locations=(Location(path="feature.txt", line=None, description=None),),
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


def test_location_nullable_fields_must_be_explicitly_present() -> None:
    with pytest.raises(ValidationError):
        Location.model_validate({"path": "feature.txt"})

    location = Location.model_validate({"path": "feature.txt", "line": None, "description": None})

    assert location.line is None
    assert location.description is None
    required_fields = set(Location.model_json_schema()["required"])
    assert {"path", "line", "description"} == required_fields


def test_finding_models_enforce_all_declared_string_and_collection_bounds() -> None:
    location = Location(path="feature.txt", line=None, description=None)
    valid_finding = {
        "severity": "important",
        "title": "Title",
        "locations": (location,),
        "evidence": "Evidence",
        "impact": "Impact",
        "suggested_fix": "Fix",
    }

    with pytest.raises(ValidationError):
        Location(path="x" * 513, line=None, description=None)
    with pytest.raises(ValidationError):
        Location(path="feature.txt", line=None, description="x" * 241)
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
            "locations": (Location(path="feature.txt", line=None, description=payload),),
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
        locations=tuple(
            Location(path=fill * 512, line=None, description=fill * 240) for _ in range(3)
        ),
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

    publish_review_result(clean_result, publisher, installation_id=23)
    assert publisher.comments == [("octo-org/example", 17, render_review_comment(clean_result))]

    publisher.comments.clear()
    publish_review_result(findings_result, publisher, installation_id=23)
    assert publisher.comments == [("octo-org/example", 17, render_review_comment(findings_result))]


def test_a_failed_review_produces_no_publishable_comment(tmp_path: Path) -> None:
    source, _, base_sha, head_sha = _diverged_repository(tmp_path)
    publisher = CapturingPublisher()
    reviewer = Reviewer(
        repository="octo-org/example",
        source_repository=source,
        resources=_resources(tmp_path / "workspaces"),
        candidate_acceptance=_acceptance(
            ReturningAdapter(
                AgentReview(
                    findings=(
                        _finding().model_copy(
                            update={
                                "locations": (
                                    Location(
                                        path="not-real.txt",
                                        line=None,
                                        description=None,
                                    ),
                                )
                            },
                        ),
                    )
                )
                .model_dump_json()
                .encode()
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
