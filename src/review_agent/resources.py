import re
import shutil
from dataclasses import dataclass
from os.path import lexists
from pathlib import Path
from typing import Protocol

WORKSPACE_PREFIX = "review-agent-workspace-"

_ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
_SANDBOX_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{2,30}-[0-9a-f]{32}$")


def _validate_workspace_root(workspace_root: Path) -> None:
    if (
        not workspace_root.is_absolute()
        or workspace_root == Path(workspace_root.anchor)
        or workspace_root.is_symlink()
    ):
        message = "workspace root must be an absolute non-root directory"
        raise ValueError(message)


class SandboxResourceClient(Protocol):
    def list_names(self) -> tuple[str, ...]: ...

    def remove(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AttemptResources:
    attempt_id: str
    workspace_root: Path
    workspace: Path
    sandbox_prefix: str
    sandbox_name: str

    def __post_init__(self) -> None:
        _validate_workspace_root(self.workspace_root)
        if _ATTEMPT_ID.fullmatch(self.attempt_id) is None:
            message = "attempt identity must be 32-character lowercase UUID hexadecimal"
            raise ValueError(message)
        if (
            self.workspace.parent != self.workspace_root
            or self.workspace.name != f"{WORKSPACE_PREFIX}{self.attempt_id}"
        ):
            message = "workspace must be the exact application-owned attempt path"
            raise ValueError(message)
        if (
            _SANDBOX_NAME.fullmatch(f"{self.sandbox_prefix}{'0' * 32}") is None
            or self.sandbox_name != f"{self.sandbox_prefix}{self.attempt_id}"
        ):
            message = "sandbox name must be the exact application-owned attempt name"
            raise ValueError(message)

    @classmethod
    def for_attempt(
        cls,
        attempt_id: str,
        *,
        workspace_root: Path,
        sandbox_prefix: str,
    ) -> "AttemptResources":
        if _ATTEMPT_ID.fullmatch(attempt_id) is None:
            message = "attempt identity must be 32-character lowercase UUID hexadecimal"
            raise ValueError(message)
        sample_name = f"{sandbox_prefix}{attempt_id}"
        if _SANDBOX_NAME.fullmatch(sample_name) is None:
            message = "sandbox prefix must be lowercase, bounded, and end with a hyphen"
            raise ValueError(message)
        return cls(
            attempt_id=attempt_id,
            workspace_root=workspace_root,
            workspace=workspace_root / f"{WORKSPACE_PREFIX}{attempt_id}",
            sandbox_prefix=sandbox_prefix,
            sandbox_name=sample_name,
        )


class ReviewResourceManager:
    def __init__(
        self,
        *,
        workspace_root: Path,
        sandbox_prefix: str,
        sandbox_client: SandboxResourceClient,
    ) -> None:
        sample_name = f"{sandbox_prefix}{'0' * 32}"
        if _SANDBOX_NAME.fullmatch(sample_name) is None:
            message = "sandbox prefix must be lowercase, bounded, and end with a hyphen"
            raise ValueError(message)
        _validate_workspace_root(workspace_root)
        self._workspace_root = workspace_root
        self._sandbox_prefix = sandbox_prefix
        self._sandbox_client = sandbox_client
        self._owned_sandbox = re.compile(rf"^{re.escape(self._sandbox_prefix)}[0-9a-f]{{32}}$")
        self._owned_workspace = re.compile(rf"^{re.escape(WORKSPACE_PREFIX)}[0-9a-f]{{32}}$")

    def for_attempt(self, attempt_id: str) -> AttemptResources:
        return AttemptResources.for_attempt(
            attempt_id,
            workspace_root=self._workspace_root,
            sandbox_prefix=self._sandbox_prefix,
        )

    def cleanup(self, attempt_id: str) -> None:
        resources = self.for_attempt(attempt_id)
        if self._workspace_root.is_symlink():
            message = "workspace root cannot be a symlink"
            raise ValueError(message)
        if resources.workspace.is_symlink():
            message = "owned workspace cannot be a symlink"
            raise ValueError(message)
        if lexists(resources.workspace) and not resources.workspace.is_dir():
            message = "owned workspace must be a directory"
            raise ValueError(message)
        cleanup_error: Exception | None = None
        try:
            if resources.workspace.exists():
                shutil.rmtree(resources.workspace)
        except Exception as error:  # noqa: BLE001 - cleanup attempts are independent.
            cleanup_error = error
        try:
            if resources.sandbox_name in self._sandbox_client.list_names():
                self._sandbox_client.remove(resources.sandbox_name)
        except Exception as error:  # noqa: BLE001 - preserve the first cleanup failure.
            if cleanup_error is None:
                cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

    def sweep_stale(self) -> None:
        if self._workspace_root.is_symlink() or not self._workspace_root.is_dir():
            message = "workspace root must be an existing non-symlink directory"
            raise ValueError(message)
        for sandbox_name in self._sandbox_client.list_names():
            if self._owned_sandbox.fullmatch(sandbox_name) is not None:
                self._sandbox_client.remove(sandbox_name)
        for entry in self._workspace_root.iterdir():
            if self._owned_workspace.fullmatch(entry.name) is None:
                continue
            if entry.is_symlink() or not entry.is_dir():
                message = "owned workspace cannot be safely inspected"
                raise ValueError(message)
            shutil.rmtree(entry)
