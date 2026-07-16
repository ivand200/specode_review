import os
import re
import selectors
import shutil
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from review_agent.core import ReviewContext, SandboxResourceLimits
from review_agent.deadline import remaining_review_time
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import AgentReview

_SANDBOX_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{2,30}-[0-9a-f]{32}$")
_VM_CHECKOUT = "/home/agent/review/repo"


class _ProcessOutputLimitError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProcessOptions:
    output_max_bytes: int
    stage: str
    timeout_seconds: float | None = None
    use_review_deadline: bool = True
    env: Mapping[str, str] | None = None


class ProcessRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    process_output_max_bytes: int = 1_048_576
    cleanup_timeout_seconds: float = 30
    deny_network: bool = True

    def __post_init__(self) -> None:
        if self.process_output_max_bytes <= 0 or self.cleanup_timeout_seconds <= 0:
            message = "sandbox process limits must be positive"
            raise ValueError(message)


def _run_bounded_process(  # noqa: C901, PLR0912, PLR0915
    arguments: tuple[str, ...],
    options: ProcessOptions,
) -> subprocess.CompletedProcess[bytes]:
    timeout_at = (
        None
        if options.timeout_seconds is None
        else time.monotonic() + options.timeout_seconds
    )
    process = subprocess.Popen(  # noqa: S603 - arguments are structured and never use a shell.
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=options.env,
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
            timeouts: list[float] = []
            if options.use_review_deadline:
                review_timeout = remaining_review_time(stage=options.stage)
                if review_timeout is not None:
                    timeouts.append(review_timeout)
            if timeout_at is not None:
                fixed_timeout = timeout_at - time.monotonic()
                if fixed_timeout <= 0:
                    raise TimeoutError
                timeouts.append(fixed_timeout)
            select_timeout = min(timeouts) if timeouts else None
            for key, _events in selector.select(select_timeout):
                remaining = options.output_max_bytes - total_bytes
                chunk = os.read(key.fd, min(65_536, remaining + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                if total_bytes > options.output_max_bytes:
                    exceeded = True
                    process.kill()
                    break
                captured[str(key.data)].extend(chunk)
            if exceeded:
                break
        wait_timeout: float | None
        if timeout_at is not None:
            wait_timeout = timeout_at - time.monotonic()
            if wait_timeout <= 0:
                raise TimeoutError
        else:
            wait_timeout = (
                remaining_review_time(stage=options.stage)
                if options.use_review_deadline
                else None
            )
        try:
            return_code = process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error
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


def _default_sbx_environment() -> dict[str, str]:
    allowed_names = {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed_names or name.startswith("DOCKER_SANDBOXES_")
    }


class DockerSandboxClient:
    def __init__(
        self,
        *,
        executable: Path | None = None,
        process_runner: ProcessRunner = _run_bounded_process,
        environment: Mapping[str, str] | None = None,
        config: DockerSandboxConfig | None = None,
    ) -> None:
        resolved_executable = executable or (
            Path(found) if (found := shutil.which("sbx")) is not None else None
        )
        if resolved_executable is None:
            message = "sbx executable is required"
            raise ValueError(message)
        resolved_config = config or DockerSandboxConfig()
        self._executable = str(resolved_executable)
        self._run_process = process_runner
        self._process_output_max_bytes = resolved_config.process_output_max_bytes
        self._cleanup_timeout_seconds = resolved_config.cleanup_timeout_seconds
        self._environment = dict(
            _default_sbx_environment() if environment is None else environment
        )
        self._deny_network = resolved_config.deny_network

    def create(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        resources: SandboxResourceLimits,
    ) -> None:
        self._run_process(
            (
                self._executable,
                "create",
                "--quiet",
                "--name",
                name,
                "--cpus",
                str(resources.cpus),
                "--memory",
                f"{resources.memory_mib}m",
                "shell",
                str(control),
                f"{checkout}:ro",
            ),
            ProcessOptions(
                output_max_bytes=self._process_output_max_bytes,
                stage="sandbox_create",
                env=self._environment,
            ),
        )
        if self._deny_network:
            self._run_process(
                (
                    self._executable,
                    "policy",
                    "deny",
                    "network",
                    "--sandbox",
                    name,
                    "**",
                ),
                ProcessOptions(
                    output_max_bytes=self._process_output_max_bytes,
                    stage="sandbox_network_policy",
                    env=self._environment,
                ),
            )

    def execute(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        workdir: str | None,
        process_limit: int,
    ) -> bytes:
        workdir_arguments = () if workdir is None else ("--workdir", workdir)
        completed = self._run_process(
            (
                self._executable,
                "exec",
                *workdir_arguments,
                name,
                "prlimit",
                f"--nproc={process_limit}",
                "--",
                *command,
            ),
            ProcessOptions(
                output_max_bytes=self._process_output_max_bytes,
                stage="sandbox_execute",
                env=self._environment,
            ),
        )
        return completed.stdout

    def remove(self, name: str) -> None:
        self._run_process(
            (self._executable, "rm", "--force", name),
            ProcessOptions(
                output_max_bytes=self._process_output_max_bytes,
                stage="sandbox_cleanup",
                timeout_seconds=self._cleanup_timeout_seconds,
                use_review_deadline=False,
                env=self._environment,
            ),
        )

    def list_names(self) -> tuple[str, ...]:
        completed = self._run_process(
            (self._executable, "ls", "--quiet"),
            ProcessOptions(
                output_max_bytes=self._process_output_max_bytes,
                stage="sandbox_list",
                env=self._environment,
            ),
        )
        return tuple(completed.stdout.decode("utf-8").splitlines())


class SandboxClient(Protocol):
    def create(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        resources: SandboxResourceLimits,
    ) -> None: ...

    def execute(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        workdir: str | None,
        process_limit: int,
    ) -> bytes: ...

    def remove(self, name: str) -> None: ...

    def list_names(self) -> tuple[str, ...]: ...


class SandboxLifecycleRunner:
    def __init__(
        self,
        *,
        client: SandboxClient,
        sandbox_prefix: str,
        review_command: tuple[str, ...] | None = None,
    ) -> None:
        sample_name = f"{sandbox_prefix}{'0' * 32}"
        if _SANDBOX_NAME_PATTERN.fullmatch(sample_name) is None:
            message = "sandbox prefix must be lowercase, bounded, and end with a hyphen"
            raise ValueError(message)
        self._client = client
        self._sandbox_prefix = sandbox_prefix
        self._review_command = review_command

    def sweep_orphans(self) -> None:
        owned_name = re.compile(rf"^{re.escape(self._sandbox_prefix)}[0-9a-f]{{32}}$")
        try:
            for sandbox_name in self._client.list_names():
                if owned_name.fullmatch(sandbox_name) is not None:
                    self._client.remove(sandbox_name)
        except Exception as error:
            raise ReviewError(
                FailureCategory.SANDBOX_LIFECYCLE,
                stage="sandbox_orphan_sweep",
            ) from error

    def run(self, context: ReviewContext) -> object:
        sandbox_name = f"{self._sandbox_prefix}{uuid.uuid4().hex}"
        control = context.workspace / "control"
        control.mkdir(mode=0o700)
        primary_failed = False
        try:
            self._client.create(
                name=sandbox_name,
                control=control,
                checkout=context.checkout,
                resources=context.sandbox_resources,
            )
            self._execute(
                sandbox_name,
                context,
                (
                    "sh",
                    "-c",
                    'if touch "$1/.review-agent-write-probe"; then exit 73; fi',
                    "review-agent-read-only-check",
                    str(context.checkout),
                ),
            )
            self._execute(
                sandbox_name,
                context,
                ("mkdir", "-p", _VM_CHECKOUT),
            )
            self._execute(
                sandbox_name,
                context,
                ("cp", "-a", f"{context.checkout}/.", _VM_CHECKOUT),
            )
            copied_head = self._execute(
                sandbox_name,
                context,
                ("git", "rev-parse", "HEAD"),
                workdir=_VM_CHECKOUT,
            ).decode("ascii").strip()
            self._ensure_exact_head(copied_head, context.request.head_sha)
            if self._review_command is not None:
                return self._execute(
                    sandbox_name,
                    context,
                    self._review_command,
                    workdir=_VM_CHECKOUT,
                )
            return AgentReview(findings=())
        except ReviewError:
            primary_failed = True
            raise
        except TimeoutError as error:
            primary_failed = True
            raise ReviewError(
                FailureCategory.TIMEOUT,
                stage="sandbox_lifecycle",
            ) from error
        except Exception as error:
            primary_failed = True
            raise ReviewError(
                FailureCategory.SANDBOX_LIFECYCLE,
                stage="sandbox_lifecycle",
            ) from error
        except BaseException:
            primary_failed = True
            raise
        finally:
            try:
                self._client.remove(sandbox_name)
            except Exception as error:
                if not primary_failed:
                    raise ReviewError(
                        FailureCategory.SANDBOX_LIFECYCLE,
                        stage="sandbox_cleanup",
                    ) from error

    def _execute(
        self,
        sandbox_name: str,
        context: ReviewContext,
        command: tuple[str, ...],
        *,
        workdir: str | None = None,
    ) -> bytes:
        return self._client.execute(
            name=sandbox_name,
            command=command,
            workdir=workdir,
            process_limit=context.sandbox_resources.pids,
        )

    @staticmethod
    def _ensure_exact_head(actual_head: str, expected_head: str) -> None:
        if actual_head != expected_head:
            raise ReviewError(
                FailureCategory.SANDBOX_LIFECYCLE,
                stage="sandbox_head_verification",
            )
