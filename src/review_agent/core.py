import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from os.path import lexists
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ValidationError

from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import AgentReview, DiffRange, Location, ReviewRequest, ReviewResult


@dataclass(frozen=True, slots=True)
class ChangedPathManifest:
    diff_range: DiffRange
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewContext:
    request: ReviewRequest
    workspace: Path
    checkout: Path
    diff_range: DiffRange
    manifest: ChangedPathManifest


class ReviewRunner(Protocol):
    def run(self, context: ReviewContext) -> AgentReview: ...


class Reviewer:
    def __init__(
        self,
        *,
        repository: str,
        source_repository: Path,
        workspace_root: Path,
        runner: ReviewRunner,
    ) -> None:
        self._repository = repository
        self._source_repository = source_repository
        self._workspace_root = workspace_root
        self._runner = runner
        git_executable = shutil.which("git")
        if git_executable is None:
            raise ReviewError(
                FailureCategory.REPOSITORY_MATERIALIZATION,
                stage="git_configuration",
            )
        self._git_executable = git_executable

    def review(self, request: ReviewRequest) -> ReviewResult:
        if request.repository != self._repository:
            msg = "request repository does not match the configured repository"
            raise ValueError(msg)

        self._workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="review-", dir=self._workspace_root))
        checkout = workspace / "checkout"
        try:
            try:
                self._clone(checkout)
                self._git(checkout, "cat-file", "-e", f"{request.base_sha}^{{commit}}")
                self._git(checkout, "cat-file", "-e", f"{request.head_sha}^{{commit}}")
                self._git(checkout, "checkout", "--detach", request.head_sha)
                checked_out_head = self._git(checkout, "rev-parse", "HEAD")
                self._ensure_exact_head(checked_out_head, request.head_sha)

                merge_base = self._git(
                    checkout,
                    "merge-base",
                    request.base_sha,
                    request.head_sha,
                )
                diff_range = DiffRange(start_sha=merge_base, end_sha=request.head_sha)
                manifest = ChangedPathManifest(
                    diff_range=diff_range,
                    paths=self._changed_paths(checkout, diff_range),
                )
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise ReviewError(
                    FailureCategory.REPOSITORY_MATERIALIZATION,
                    stage="repository_materialization",
                ) from error
            context = ReviewContext(
                request=request,
                workspace=workspace,
                checkout=checkout,
                diff_range=diff_range,
                manifest=manifest,
            )
            try:
                raw_candidate = self._runner.run(context)
            except TimeoutError as error:
                raise ReviewError(
                    FailureCategory.TIMEOUT,
                    stage="review_runner",
                ) from error
            try:
                candidate_input = (
                    raw_candidate.model_dump(mode="python")
                    if isinstance(raw_candidate, AgentReview)
                    else raw_candidate
                )
                candidate = AgentReview.model_validate(candidate_input)
            except ValidationError as error:
                raise ReviewError(
                    FailureCategory.INVALID_MODEL_OUTPUT,
                    stage="candidate_validation",
                ) from error
            try:
                self._ground_candidate(checkout, manifest, candidate)
            except (OSError, RuntimeError, ValueError) as error:
                raise ReviewError(
                    FailureCategory.INVALID_MODEL_OUTPUT,
                    stage="candidate_grounding",
                ) from error
            status: Literal["issues_found", "no_important_issues"] = (
                "issues_found" if candidate.findings else "no_important_issues"
            )
            return ReviewResult(
                repository=request.repository,
                pr_number=request.pr_number,
                diff_range=diff_range,
                status=status,
                findings=candidate.findings,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _clone(self, checkout: Path) -> None:
        subprocess.run(  # noqa: S603 - arguments are structured and commit SHAs are validated.
            [
                self._git_executable,
                "clone",
                "--no-checkout",
                "--no-local",
                str(self._source_repository),
                str(checkout),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _git(self, repository: Path, *arguments: str) -> str:
        completed = subprocess.run(  # noqa: S603 - never invokes a shell.
            [self._git_executable, "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _ensure_exact_head(actual_head: str, expected_head: str) -> None:
        if actual_head != expected_head:
            msg = "checked out commit does not match the accepted head commit"
            raise ValueError(msg)

    def _changed_paths(self, checkout: Path, diff_range: DiffRange) -> tuple[str, ...]:
        output = self._git(
            checkout,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            diff_range.start_sha,
            diff_range.end_sha,
        )
        if not output:
            return ()
        return tuple(output.removesuffix("\0").split("\0"))

    @staticmethod
    def _ground_candidate(
        checkout: Path,
        manifest: ChangedPathManifest,
        candidate: AgentReview,
    ) -> None:
        checkout_root = checkout.resolve()
        changed_paths = frozenset(manifest.paths)
        for finding in candidate.findings:
            has_changed_location = False
            for location in finding.locations:
                is_changed = Reviewer._ground_location(
                    checkout,
                    checkout_root,
                    changed_paths,
                    location,
                )
                has_changed_location = has_changed_location or is_changed

            if not has_changed_location:
                msg = "each finding requires at least one changed-path location"
                raise ValueError(msg)

    @staticmethod
    def _ground_location(
        checkout: Path,
        checkout_root: Path,
        changed_paths: frozenset[str],
        location: Location,
    ) -> bool:
        relative_path = PurePosixPath(location.path)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\x00" in location.path:
            msg = "location path must remain inside the checkout"
            raise ValueError(msg)

        candidate_path = checkout.joinpath(*relative_path.parts)
        try:
            candidate_path.resolve(strict=False).relative_to(checkout_root)
        except ValueError as error:
            msg = "location path escapes the checkout"
            raise ValueError(msg) from error

        is_changed = location.path in changed_paths
        path_exists = lexists(candidate_path)
        if not path_exists and not is_changed:
            msg = "location path does not exist at the reviewed head"
            raise ValueError(msg)
        if path_exists and not (candidate_path.is_file() or candidate_path.is_symlink()):
            msg = "location path must identify a file"
            raise ValueError(msg)

        if location.line is None:
            return is_changed
        if not path_exists or not candidate_path.is_file():
            msg = "line locations require a file at the reviewed head"
            raise ValueError(msg)
        contents = candidate_path.read_bytes()
        if b"\x00" in contents:
            msg = "line locations cannot reference binary files"
            raise ValueError(msg)
        if location.line > len(contents.splitlines()):
            msg = "location line is outside the reviewed head file"
            raise ValueError(msg)
        return is_changed
