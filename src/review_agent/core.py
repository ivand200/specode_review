import json
import os
import selectors
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from os.path import lexists
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ValidationError

from review_agent.deadline import remaining_review_time
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import AgentReview, DiffRange, Location, ReviewRequest, ReviewResult

MAX_CHANGED_FILES = 100
MAX_CHANGED_TEXT_LINES = 5_000
CANDIDATE_OUTPUT_MAX_BYTES = 65_536
PROCESS_OUTPUT_MAX_BYTES = 1_048_576


class _ProcessOutputLimitError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ChangedPathManifest:
    diff_range: DiffRange
    paths: tuple[str, ...]
    changed_files: int
    changed_text_lines: int


@dataclass(frozen=True, slots=True)
class SandboxResourceLimits:
    cpus: int = 2
    memory_mib: int = 4_096
    pids: int = 256

    def __post_init__(self) -> None:
        if self.cpus <= 0 or self.memory_mib <= 0 or self.pids <= 0:
            message = "sandbox resource limits must be positive"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ReviewLimits:
    process_output_max_bytes: int = PROCESS_OUTPUT_MAX_BYTES
    sandbox_resources: SandboxResourceLimits = field(default_factory=SandboxResourceLimits)

    def __post_init__(self) -> None:
        if self.process_output_max_bytes <= 0:
            message = "process output limit must be positive"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ReviewContext:
    request: ReviewRequest
    workspace: Path
    checkout: Path
    diff_range: DiffRange
    manifest: ChangedPathManifest
    sandbox_resources: SandboxResourceLimits


class ReviewRunner(Protocol):
    def run(self, context: ReviewContext) -> object: ...


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
        workspace_root: Path,
        runner: ReviewRunner,
        source_repository: Path | GitHubRepository,
        limits: ReviewLimits | None = None,
    ) -> None:
        self._repository = repository
        self._source_repository = source_repository
        self._workspace_root = workspace_root
        self._runner = runner
        self._limits = limits or ReviewLimits()
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

        self._workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="review-", dir=self._workspace_root))
        checkout = workspace / "checkout"
        try:
            context = self._prepare_context(workspace, checkout, request)
            candidate = self._candidate_from_runner(context)
            try:
                remaining_review_time(stage="candidate_grounding")
                self._ground_candidate(checkout, context.manifest, candidate)
                remaining_review_time(stage="candidate_grounding")
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
                diff_range=context.diff_range,
                status=status,
                findings=candidate.findings,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

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
        return ReviewContext(
            request=request,
            workspace=workspace,
            checkout=checkout,
            diff_range=diff_range,
            manifest=manifest,
            sandbox_resources=self._limits.sandbox_resources,
        )

    def _candidate_from_runner(self, context: ReviewContext) -> AgentReview:
        remaining_review_time(stage="review_runner")
        try:
            raw_candidate = self._runner.run(context)
        except TimeoutError as error:
            raise ReviewError(FailureCategory.TIMEOUT, stage="review_runner") from error
        remaining_review_time(stage="review_runner")
        try:
            candidate_bytes = self._candidate_bytes(raw_candidate)
        except (TypeError, ValueError) as error:
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_validation",
            ) from error
        if len(candidate_bytes) > CANDIDATE_OUTPUT_MAX_BYTES:
            raise ReviewError(FailureCategory.CODEX_OR_LIMIT, stage="candidate_output")
        try:
            return AgentReview.model_validate_json(candidate_bytes)
        except ValidationError as error:
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_validation",
            ) from error

    @staticmethod
    def _candidate_bytes(raw_candidate: object) -> bytes:
        if isinstance(raw_candidate, bytes):
            return raw_candidate
        if isinstance(raw_candidate, str):
            return raw_candidate.encode("utf-8")
        if isinstance(raw_candidate, AgentReview):
            return raw_candidate.model_dump_json().encode("utf-8")
        return json.dumps(
            raw_candidate,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

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
            '  *Password*) printf "%s\\n" "$REVIEW_AGENT_GITHUB_TOKEN" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "REVIEW_AGENT_GITHUB_TOKEN": token,
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
        completed = self._run_process(
            (self._git_executable, "-C", str(repository), *arguments),
        )
        return completed.stdout.decode("utf-8").strip()

    def _run_process(
        self,
        arguments: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        remaining_review_time(stage="repository_materialization")
        process = subprocess.Popen(  # noqa: S603 - arguments are structured and never use a shell.
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        if process.stdout is None or process.stderr is None:
            message = "bounded process capture requires stdout and stderr pipes"
            raise RuntimeError(message)

        captured = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        total_bytes = 0
        exceeded = False
        try:
            while selector.get_map():
                timeout = remaining_review_time(stage="repository_materialization")
                for key, _events in selector.select(timeout):
                    remaining = self._limits.process_output_max_bytes - total_bytes
                    chunk = os.read(key.fd, min(65_536, remaining + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > self._limits.process_output_max_bytes:
                        exceeded = True
                        process.kill()
                        break
                    captured[str(key.data)].extend(chunk)
                if exceeded:
                    break
            return_code = self._wait_for_process(process)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

        if exceeded:
            raise _ProcessOutputLimitError
        completed = subprocess.CompletedProcess(
            arguments,
            return_code,
            stdout=bytes(captured["stdout"]),
            stderr=bytes(captured["stderr"]),
        )
        if return_code != 0:
            raise subprocess.CalledProcessError(
                return_code,
                arguments,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed

    @staticmethod
    def _wait_for_process(process: subprocess.Popen[bytes]) -> int:
        wait_timeout = remaining_review_time(stage="repository_materialization")
        try:
            return process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired as error:
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
