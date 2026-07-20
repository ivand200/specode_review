import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from os.path import lexists
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ValidationError

from specode_review.configuration import ReviewLimits, SandboxResourceLimits
from specode_review.deadline import remaining_review_time
from specode_review.errors import FailureCategory, ReviewError
from specode_review.models import AgentReview, DiffRange, Location, ReviewRequest, ReviewResult
from specode_review.process import ProcessOptions, _run_bounded_process
from specode_review.resources import AttemptResources

MAX_CHANGED_FILES = 100
MAX_CHANGED_TEXT_LINES = 5_000


@dataclass(frozen=True, slots=True)
class ChangedPathManifest:
    diff_range: DiffRange
    paths: tuple[str, ...]
    changed_files: int
    changed_text_lines: int


@dataclass(frozen=True, slots=True)
class ReviewContext:
    request: ReviewRequest
    workspace: Path
    checkout: Path
    diff_range: DiffRange
    manifest: ChangedPathManifest
    sandbox_resources: SandboxResourceLimits
    primary_diff: bytes = b""


@dataclass(frozen=True, slots=True)
class CandidateContract:
    schema_json: bytes
    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            message = "candidate output limit must be positive"
            raise ValueError(message)


class _CandidateAdapter(Protocol):
    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes: ...


class CandidateAcceptance:
    def __init__(self, *, adapter: _CandidateAdapter, max_bytes: int) -> None:
        if max_bytes <= 0:
            message = "candidate output limit must be positive"
            raise ValueError(message)
        schema = AgentReview.model_json_schema()
        self._verify_schema_invariants(schema)
        schema_json = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._adapter = adapter
        self._contract = CandidateContract(schema_json=schema_json, max_bytes=max_bytes)

    def accept(self, context: ReviewContext) -> AgentReview:
        remaining_review_time(stage="review_runner")
        try:
            candidate_bytes = self._adapter.produce(context, self._contract)
        except TimeoutError as error:
            raise ReviewError(FailureCategory.TIMEOUT, stage="review_runner") from error
        remaining_review_time(stage="review_runner")
        if not isinstance(candidate_bytes, bytes):
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_validation",
            )
        if len(candidate_bytes) > self._contract.max_bytes:
            raise ReviewError(FailureCategory.CODEX_OR_LIMIT, stage="candidate_output")
        try:
            candidate = AgentReview.model_validate_json(candidate_bytes, strict=True)
        except ValidationError as error:
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_validation",
            ) from error
        try:
            remaining_review_time(stage="candidate_grounding")
            self._ground_candidate(context.checkout, context.manifest, candidate)
            remaining_review_time(stage="candidate_grounding")
        except (OSError, RuntimeError, ValueError) as error:
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_grounding",
            ) from error
        return candidate

    @classmethod
    def _verify_schema_invariants(cls, node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if node.get("type") == "object" or isinstance(properties, dict):
                declared_properties = properties if isinstance(properties, dict) else {}
                required = node.get("required")
                if (
                    node.get("additionalProperties") is not False
                    or not isinstance(required, list)
                    or len(required) != len(declared_properties)
                    or set(required) != set(declared_properties)
                ):
                    message = "candidate schema object invariants are not satisfied"
                    raise RuntimeError(message)
            for value in node.values():
                cls._verify_schema_invariants(value)
        elif isinstance(node, list):
            for value in node:
                cls._verify_schema_invariants(value)

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
                is_changed = CandidateAcceptance._ground_location(
                    checkout,
                    checkout_root,
                    changed_paths,
                    location,
                )
                has_changed_location = has_changed_location or is_changed

            if not has_changed_location:
                message = "each finding requires at least one changed-path location"
                raise ValueError(message)

    @staticmethod
    def _ground_location(
        checkout: Path,
        checkout_root: Path,
        changed_paths: frozenset[str],
        location: Location,
    ) -> bool:
        relative_path = PurePosixPath(location.path)
        if relative_path.is_absolute() or ".." in relative_path.parts or "\x00" in location.path:
            message = "location path must remain inside the checkout"
            raise ValueError(message)

        candidate_path = checkout.joinpath(*relative_path.parts)
        try:
            candidate_path.resolve(strict=False).relative_to(checkout_root)
        except ValueError as error:
            message = "location path escapes the checkout"
            raise ValueError(message) from error

        is_changed = location.path in changed_paths
        path_exists = lexists(candidate_path)
        if not path_exists and not is_changed:
            message = "location path does not exist at the reviewed head"
            raise ValueError(message)
        if path_exists and not (candidate_path.is_file() or candidate_path.is_symlink()):
            message = "location path must identify a file"
            raise ValueError(message)

        if location.line is None:
            return is_changed
        if not path_exists or not candidate_path.is_file():
            message = "line locations require a file at the reviewed head"
            raise ValueError(message)
        contents = candidate_path.read_bytes()
        if b"\x00" in contents:
            message = "line locations cannot reference binary files"
            raise ValueError(message)
        if location.line > len(contents.splitlines()):
            message = "location line is outside the reviewed head file"
            raise ValueError(message)
        return is_changed


class InstallationCredentials(Protocol):
    def installation_token(self, *, repository: str, installation_id: int) -> str: ...


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    credentials: InstallationCredentials
    clone_url: str | None = None


class Reviewer:
    def __init__(
        self,
        *,
        repository: str,
        resources: AttemptResources,
        candidate_acceptance: CandidateAcceptance,
        source_repository: Path | GitHubRepository,
        limits: ReviewLimits | None = None,
    ) -> None:
        self._repository = repository
        self._source_repository = source_repository
        self._workspace = resources.workspace
        self._candidate_acceptance = candidate_acceptance
        self._limits = limits or ReviewLimits()
        self._workspace.parent.mkdir(parents=True, exist_ok=True)
        git_executable = shutil.which("git")
        if git_executable is None:
            raise ReviewError(
                FailureCategory.REPOSITORY_MATERIALIZATION,
                stage="git_configuration",
            )
        self._git_executable = git_executable

    def review(self, request: ReviewRequest) -> ReviewResult:
        remaining_review_time(stage="review")
        if request.repository != self._repository:
            msg = "request repository does not match the configured repository"
            raise ValueError(msg)

        workspace = self._create_workspace()
        checkout = workspace / "checkout"
        primary_failed = False
        try:
            context = self._prepare_context(workspace, checkout, request)
            candidate = self._candidate_acceptance.accept(context)
            status: Literal["issues_found", "no_important_issues"] = (
                "issues_found" if candidate.findings else "no_important_issues"
            )
            return ReviewResult(
                repository=request.repository,
                pr_number=request.pr_number,
                diff_range=context.diff_range,
                status=status,
                findings=candidate.findings,
            )
        except BaseException:
            primary_failed = True
            raise
        finally:
            try:
                shutil.rmtree(workspace)
            except Exception as error:
                if not primary_failed:
                    raise ReviewError(
                        FailureCategory.REVIEW_FAILURE,
                        stage="workspace_cleanup",
                    ) from error

    def _create_workspace(self) -> Path:
        try:
            self._workspace.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ReviewError(
                FailureCategory.REVIEW_FAILURE,
                stage="workspace_allocation",
            ) from error
        return self._workspace

    def _prepare_context(
        self,
        workspace: Path,
        checkout: Path,
        request: ReviewRequest,
    ) -> ReviewContext:
        try:
            self._materialize_repository(workspace, checkout, request)
            self._git(checkout, "cat-file", "-e", f"{request.base_sha}^{{commit}}")
            self._git(checkout, "cat-file", "-e", f"{request.head_sha}^{{commit}}")
            self._git(checkout, "checkout", "--detach", request.head_sha)
            checked_out_head = self._git(checkout, "rev-parse", "HEAD")
            self._ensure_exact_head(checked_out_head, request.head_sha)
            merge_base = self._git(checkout, "merge-base", request.base_sha, request.head_sha)
            diff_range = DiffRange(start_sha=merge_base, end_sha=request.head_sha)
            changed_paths = self._changed_paths(checkout, diff_range)
            manifest = ChangedPathManifest(
                diff_range=diff_range,
                paths=changed_paths,
                changed_files=len(changed_paths),
                changed_text_lines=self._changed_text_lines(checkout, diff_range),
            )
        except ReviewError:
            raise
        except Exception as error:
            raise ReviewError(
                FailureCategory.REPOSITORY_MATERIALIZATION,
                stage="repository_materialization",
            ) from error
        if (
            manifest.changed_files > MAX_CHANGED_FILES
            or manifest.changed_text_lines > MAX_CHANGED_TEXT_LINES
        ):
            raise ReviewError(FailureCategory.REVIEW_TOO_LARGE, stage="review_size")
        primary_diff = self._primary_diff(checkout, diff_range)
        return ReviewContext(
            request=request,
            workspace=workspace,
            checkout=checkout,
            diff_range=diff_range,
            manifest=manifest,
            sandbox_resources=self._limits.sandbox_resources,
            primary_diff=primary_diff,
        )

    def _materialize_repository(
        self,
        workspace: Path,
        checkout: Path,
        request: ReviewRequest,
    ) -> None:
        if isinstance(self._source_repository, Path):
            self._clone_local(checkout)
            return
        self._clone_github(workspace, checkout, request, self._source_repository)

    def _clone_local(self, checkout: Path) -> None:
        if not isinstance(self._source_repository, Path):
            message = "local repository source is not configured"
            raise TypeError(message)
        self._run_process(
            (
                self._git_executable,
                "clone",
                "--no-checkout",
                "--no-local",
                str(self._source_repository),
                str(checkout),
            ),
        )

    def _clone_github(
        self,
        workspace: Path,
        checkout: Path,
        request: ReviewRequest,
        source: GitHubRepository,
    ) -> None:
        token = source.credentials.installation_token(
            repository=request.repository,
            installation_id=request.installation_id,
        )
        clone_url = source.clone_url or f"https://github.com/{self._repository}.git"
        askpass = workspace / "github-askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
            '  *Password*) printf "%s\\n" "$SPECODE_REVIEW_GITHUB_TOKEN" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "SPECODE_REVIEW_GITHUB_TOKEN": token,
        }
        try:
            self._authenticated_git(
                environment,
                "clone",
                "--no-checkout",
                "--no-local",
                clone_url,
                str(checkout),
            )
            self._authenticated_git(
                environment,
                "-C",
                str(checkout),
                "fetch",
                "--no-tags",
                "origin",
                request.base_sha,
            )
            self._authenticated_git(
                environment,
                "-C",
                str(checkout),
                "fetch",
                "--no-tags",
                "origin",
                f"refs/pull/{request.pr_number}/head",
            )
        finally:
            askpass.unlink(missing_ok=True)

    def _authenticated_git(self, environment: dict[str, str], *arguments: str) -> None:
        self._run_process(
            (
                self._git_executable,
                "-c",
                "credential.helper=",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ),
            env=environment,
        )

    def _git(self, repository: Path, *arguments: str) -> str:
        return self._git_bytes(repository, *arguments).decode("utf-8").strip()

    def _git_bytes(self, repository: Path, *arguments: str) -> bytes:
        completed = self._run_process(
            (self._git_executable, "-C", str(repository), *arguments),
        )
        return completed.stdout

    def _run_process(
        self,
        arguments: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        remaining_review_time(stage="repository_materialization")
        try:
            return _run_bounded_process(
                arguments,
                ProcessOptions(
                    output_max_bytes=self._limits.process_output_max_bytes,
                    stage="repository_materialization",
                    env=env,
                ),
            )
        except TimeoutError as error:
            raise ReviewError(
                FailureCategory.TIMEOUT,
                stage="repository_materialization",
            ) from error

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

    def _changed_text_lines(self, checkout: Path, diff_range: DiffRange) -> int:
        output = self._git(
            checkout,
            "diff",
            "--numstat",
            "--no-renames",
            "-z",
            diff_range.start_sha,
            diff_range.end_sha,
        )
        if not output:
            return 0

        changed_lines = 0
        for record in output.removesuffix("\0").split("\0"):
            added, deleted, _path = record.split("\t", maxsplit=2)
            if added == "-" or deleted == "-":
                continue
            changed_lines += int(added) + int(deleted)
        return changed_lines

    def _primary_diff(self, checkout: Path, diff_range: DiffRange) -> bytes:
        return self._git_bytes(
            checkout,
            "diff",
            "--no-ext-diff",
            "--no-renames",
            diff_range.start_sha,
            diff_range.end_sha,
            "--",
        )
