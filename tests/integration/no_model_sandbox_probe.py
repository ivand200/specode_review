"""Test-only Docker Sandbox probe that never invokes Codex or a model."""

import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from specode_review.configuration import SandboxOperationPolicy, SandboxResourceLimits
from specode_review.core import CandidateContract, ReviewContext
from specode_review.errors import FailureCategory, ReviewError
from specode_review.process import ProcessOptions, ProcessRunner, _run_bounded_process
from specode_review.resources import AttemptResources

_VM_CHECKOUT = "/home/agent/review/repo"
_SUCCESS_COMMAND = (
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
_TIMEOUT_COMMAND = ("sleep", "30")


def _sbx_environment() -> dict[str, str]:
    allowed_names = {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed_names or name.startswith("DOCKER_SANDBOXES_")
    }


class NoModelDockerSandboxProbe:
    """Own shell-sandbox operations used only by the opt-in Docker test profile."""

    def __init__(
        self,
        *,
        executable: Path | None = None,
        process_runner: ProcessRunner = _run_bounded_process,
        environment: Mapping[str, str] | None = None,
        config: SandboxOperationPolicy | None = None,
    ) -> None:
        resolved_executable = executable or (
            Path(found) if (found := shutil.which("sbx")) is not None else None
        )
        if resolved_executable is None:
            message = "sbx executable is required for the no-model Docker probe"
            raise ValueError(message)
        resolved_config = config or SandboxOperationPolicy()
        self._executable = str(resolved_executable)
        self._run_process = process_runner
        self._output_max_bytes = resolved_config.process_output_max_bytes
        self._operation_timeout_seconds = resolved_config.cleanup_timeout_seconds
        self._environment = dict(_sbx_environment() if environment is None else environment)
        self._deny_network = resolved_config.deny_network

    def candidate_adapter(
        self,
        *,
        resources: AttemptResources,
        timeout: bool = False,
    ) -> "_NoModelCandidateAdapter":
        return _NoModelCandidateAdapter(
            probe=self,
            resources=resources,
            command=_TIMEOUT_COMMAND if timeout else _SUCCESS_COMMAND,
        )

    def create_stale(
        self,
        *,
        resources: AttemptResources,
        control: Path,
        checkout: Path,
        limits: SandboxResourceLimits | None = None,
    ) -> None:
        self._create(
            name=resources.sandbox_name,
            control=control,
            checkout=checkout,
            resources=limits or SandboxResourceLimits(),
        )

    def list_names(self) -> tuple[str, ...]:
        completed = self._run(
            (self._executable, "ls", "--quiet"),
            stage="sandbox_list",
            use_review_deadline=False,
        )
        return tuple(completed.stdout.decode("utf-8").splitlines())

    def remove(self, name: str) -> None:
        self._run(
            (self._executable, "rm", "--force", name),
            stage="sandbox_cleanup",
            use_review_deadline=False,
        )

    def cleanup(self, names: Iterable[str]) -> None:
        """Best-effort bounded cleanup that is safe to repeat after partial failure."""
        cleanup_error: Exception | None = None
        requested_names = frozenset(names)
        existing_names = self.list_names()
        for name in existing_names:
            if name not in requested_names:
                continue
            try:
                self.remove(name)
            except Exception as error:  # noqa: BLE001 - cleanup attempts are independent.
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

    def _produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
        *,
        resources: AttemptResources,
        command: tuple[str, ...],
    ) -> bytes:
        control = context.workspace / "no-model-probe-control"
        primary_failed = False
        try:
            control.mkdir(mode=0o700)
            self._create(
                name=resources.sandbox_name,
                control=control,
                checkout=context.checkout,
                resources=context.sandbox_resources,
            )
            self._prepare_checkout(resources.sandbox_name, context)
            candidate = self._execute(
                name=resources.sandbox_name,
                command=command,
                workdir=_VM_CHECKOUT,
                process_limit=context.sandbox_resources.pids,
            )
            return candidate[: contract.max_bytes + 1]
        except BaseException:
            primary_failed = True
            raise
        finally:
            try:
                self.remove(resources.sandbox_name)
            except Exception:
                if not primary_failed:
                    raise

    def _create(
        self,
        *,
        name: str,
        control: Path,
        checkout: Path,
        resources: SandboxResourceLimits,
    ) -> None:
        self._run(
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
            stage="sandbox_create",
        )
        if self._deny_network:
            self._run(
                (
                    self._executable,
                    "policy",
                    "deny",
                    "network",
                    "--sandbox",
                    name,
                    "**",
                ),
                stage="sandbox_network_policy",
            )

    def _prepare_checkout(self, name: str, context: ReviewContext) -> None:
        self._execute(
            name=name,
            command=(
                "sh",
                "-c",
                'if touch "$1/.specode-review-write-probe"; then exit 73; fi',
                "specode-review-read-only-check",
                str(context.checkout),
            ),
            workdir=None,
            process_limit=context.sandbox_resources.pids,
        )
        self._execute(
            name=name,
            command=("mkdir", "-p", _VM_CHECKOUT),
            workdir=None,
            process_limit=context.sandbox_resources.pids,
        )
        self._execute(
            name=name,
            command=("cp", "-R", f"{context.checkout}/.", _VM_CHECKOUT),
            workdir=None,
            process_limit=context.sandbox_resources.pids,
        )
        copied_head = self._execute(
            name=name,
            command=("git", "rev-parse", "HEAD"),
            workdir=_VM_CHECKOUT,
            process_limit=context.sandbox_resources.pids,
        )
        if copied_head.decode("ascii").strip() != context.request.head_sha:
            raise ReviewError(
                FailureCategory.SANDBOX_LIFECYCLE,
                stage="no_model_probe_head_verification",
            )

    def _execute(
        self,
        *,
        name: str,
        command: tuple[str, ...],
        workdir: str | None,
        process_limit: int,
    ) -> bytes:
        workdir_arguments = () if workdir is None else ("--workdir", workdir)
        completed = self._run(
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
            stage="sandbox_execute",
        )
        return completed.stdout

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        stage: str,
        use_review_deadline: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return self._run_process(
            arguments,
            ProcessOptions(
                output_max_bytes=self._output_max_bytes,
                stage=stage,
                timeout_seconds=self._operation_timeout_seconds,
                use_review_deadline=use_review_deadline,
                env=self._environment,
            ),
        )


class _NoModelCandidateAdapter:
    def __init__(
        self,
        *,
        probe: NoModelDockerSandboxProbe,
        resources: AttemptResources,
        command: tuple[str, ...],
    ) -> None:
        self._probe = probe
        self._resources = resources
        self._command = command

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        return self._probe._produce(  # noqa: SLF001 - adapter is part of this test helper.
            context,
            contract,
            resources=self._resources,
            command=self._command,
        )
