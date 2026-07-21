import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, Protocol

from specode_review.configuration import (
    PINNED_CODEX_VERSION,
    PINNED_SBX_VERSION,
    ProductionServiceSettings,
)
from specode_review.process import ProcessOptions, _run_bounded_process

logger = logging.getLogger(__name__)


class StartupReadinessError(RuntimeError):
    """A normalized readiness failure that never includes subprocess output."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"production startup readiness failed: {stage}")


class ReadinessProcessRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        output_max_bytes: int,
    ) -> subprocess.CompletedProcess[bytes]: ...


def _default_process_runner(
    arguments: tuple[str, ...],
    output_max_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    return _run_bounded_process(
        arguments,
        ProcessOptions(
            output_max_bytes=output_max_bytes,
            stage="startup_readiness",
            timeout_seconds=30,
            use_review_deadline=False,
            env={
                name: value
                for name, value in os.environ.items()
                if name in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
                or name.startswith("DOCKER_SANDBOXES_")
            },
        ),
    )


class ProductionReadiness:
    def __init__(
        self,
        *,
        process_runner: ReadinessProcessRunner = _default_process_runner,
        executable_resolver: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._run_process = process_runner
        self._resolve_executable = executable_resolver

    def check(
        self,
        settings: ProductionServiceSettings,
    ) -> None:
        self._verify_paths(settings)
        attempt = settings.attempt
        process_output_max_bytes = attempt.process_output_max_bytes
        sbx = self._executable("sbx")
        codex = self._executable("codex")
        git = self._executable("git")
        self._verify_version(
            (sbx, "version"),
            stage="sbx_version",
            expected=PINNED_SBX_VERSION,
            pattern=rb"\bsbx version: v([0-9]+\.[0-9]+\.[0-9]+)\b",
            output_max_bytes=process_output_max_bytes,
        )
        self._verify_version(
            (codex, "--version"),
            stage="codex_version",
            expected=PINNED_CODEX_VERSION,
            pattern=rb"\bcodex-cli ([0-9]+\.[0-9]+\.[0-9]+)\b",
            output_max_bytes=process_output_max_bytes,
        )
        self._command(
            (git, "--version"),
            stage="git_capability",
            output_max_bytes=process_output_max_bytes,
        )
        self._command(
            (sbx, "diagnose"),
            stage="sandbox_host_capability",
            output_max_bytes=process_output_max_bytes,
        )
        self._command(
            (sbx, "kit", "validate", str(attempt.review_kit_path)),
            stage="review_kit_validation",
            output_max_bytes=process_output_max_bytes,
        )

    @staticmethod
    def _verify_paths(
        settings: ProductionServiceSettings,
    ) -> None:
        attempt = settings.attempt
        try:
            configured_inputs_are_valid = not (
                attempt.private_key_path.is_symlink()
                or not attempt.private_key_path.is_file()
                or attempt.review_kit_path.is_symlink()
                or not attempt.review_kit_path.is_dir()
            )
            attempt.workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            workspace_is_valid = not (
                attempt.workspace_root.is_symlink()
                or not attempt.workspace_root.is_dir()
                or not os.access(attempt.workspace_root, os.R_OK | os.W_OK | os.X_OK)
            )
        except OSError:
            ProductionReadiness._fail("configured_paths")
        if not configured_inputs_are_valid or not workspace_is_valid:
            ProductionReadiness._fail("configured_paths")

    def _executable(self, name: str) -> str:
        executable = self._resolve_executable(name)
        if executable is None or not Path(executable).is_absolute():
            self._fail(f"{name}_executable")
        return executable

    def _verify_version(
        self,
        arguments: tuple[str, ...],
        *,
        stage: str,
        expected: str,
        pattern: bytes,
        output_max_bytes: int,
    ) -> None:
        output = self._command(
            arguments,
            stage=stage,
            output_max_bytes=output_max_bytes,
        )
        match = re.search(pattern, output)
        if match is None or match.group(1).decode("ascii") != expected:
            self._fail(stage)

    def _command(
        self,
        arguments: tuple[str, ...],
        *,
        stage: str,
        output_max_bytes: int,
    ) -> bytes:
        try:
            completed = self._run_process(arguments, output_max_bytes)
        except Exception:  # noqa: BLE001 - normalized external-process boundary.
            self._fail(stage)
        if completed.returncode != 0:
            self._fail(stage)
        return completed.stdout

    @staticmethod
    def _fail(stage: str) -> NoReturn:
        logger.error("startup readiness failed stage=%s category=startup_readiness", stage)
        raise StartupReadinessError(stage) from None
