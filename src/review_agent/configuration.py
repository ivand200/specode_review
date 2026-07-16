import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from review_agent.core import (
    CANDIDATE_OUTPUT_MAX_BYTES,
    PROCESS_OUTPUT_MAX_BYTES,
    SandboxResourceLimits,
)
from review_agent.web import DEFAULT_REVIEW_TIMEOUT_SECONDS

DEFAULT_SANDBOX_NAME_PREFIX = "review-agent-"
DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS = 30.0
PINNED_SBX_VERSION = "0.35.0"
PINNED_CODEX_VERSION = "0.144.5"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SANDBOX_PREFIX = re.compile(r"^[a-z0-9][a-z0-9.-]{1,29}-$")
_WEBHOOK_SECRET_MIN_CHARS = 32
_WEBHOOK_SECRET_MAX_CHARS = 1_024
_CODEX_MODEL_MAX_CHARS = 128


class ConfigurationError(ValueError):
    """A normalized startup configuration failure safe for logs and stderr."""

    def __init__(self, setting: str) -> None:
        self.setting = setting
        super().__init__(f"invalid startup configuration: {setting}")


def _invalid(setting: str) -> NoReturn:
    raise ConfigurationError(setting)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value:
        raise ConfigurationError(name)
    return value


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int | None = None,
) -> int:
    raw = environment.get(name)
    if raw is None and default is not None:
        return default
    try:
        value = int(_required(environment, name) if raw is None else raw)
    except ValueError:
        raise ConfigurationError(name) from None
    if value <= 0:
        raise ConfigurationError(name)
    return value


def _positive_float(
    environment: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    raw = environment.get(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(name) from None
    if value <= 0:
        raise ConfigurationError(name)
    return value


def _absolute_path(environment: Mapping[str, str], name: str) -> Path:
    path = Path(_required(environment, name))
    if not path.is_absolute():
        raise ConfigurationError(name)
    return path


def _existing_file(environment: Mapping[str, str], name: str) -> Path:
    path = _absolute_path(environment, name)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(name)
    return path


def _existing_directory(environment: Mapping[str, str], name: str) -> Path:
    path = _absolute_path(environment, name)
    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError(name)
    return path


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    repository: str
    app_id: int
    private_key_path: Path
    webhook_secret: str = field(repr=False)
    codex_model: str
    review_kit_path: Path
    workspace_root: Path
    review_timeout_seconds: float
    sandbox_resources: SandboxResourceLimits
    process_output_max_bytes: int
    candidate_output_max_bytes: int
    sandbox_cleanup_timeout_seconds: float
    sandbox_name_prefix: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ProductionSettings":
        repository = _required(environment, "GITHUB_REPOSITORY")
        if _REPOSITORY.fullmatch(repository) is None or repository.endswith(".git"):
            _invalid("GITHUB_REPOSITORY")

        webhook_secret = _required(environment, "GITHUB_WEBHOOK_SECRET")
        if not _WEBHOOK_SECRET_MIN_CHARS <= len(webhook_secret) <= _WEBHOOK_SECRET_MAX_CHARS:
            _invalid("GITHUB_WEBHOOK_SECRET")

        codex_model = _required(environment, "CODEX_MODEL")
        if len(codex_model) > _CODEX_MODEL_MAX_CHARS or codex_model.strip() != codex_model:
            _invalid("CODEX_MODEL")

        workspace_root = _absolute_path(environment, "WORKSPACE_ROOT")
        if workspace_root == Path(workspace_root.anchor) or workspace_root.is_symlink():
            _invalid("WORKSPACE_ROOT")

        sandbox_name_prefix = environment.get(
            "SANDBOX_NAME_PREFIX",
            DEFAULT_SANDBOX_NAME_PREFIX,
        )
        if _SANDBOX_PREFIX.fullmatch(sandbox_name_prefix) is None:
            _invalid("SANDBOX_NAME_PREFIX")

        return cls(
            repository=repository,
            app_id=_positive_int(environment, "GITHUB_APP_ID"),
            private_key_path=_existing_file(environment, "GITHUB_PRIVATE_KEY_PATH"),
            webhook_secret=webhook_secret,
            codex_model=codex_model,
            review_kit_path=_existing_directory(environment, "REVIEW_KIT_PATH"),
            workspace_root=workspace_root,
            review_timeout_seconds=_positive_float(
                environment,
                "REVIEW_TIMEOUT_SECONDS",
                default=DEFAULT_REVIEW_TIMEOUT_SECONDS,
            ),
            sandbox_resources=SandboxResourceLimits(
                cpus=_positive_int(environment, "SANDBOX_CPUS", default=2),
                memory_mib=_positive_int(environment, "SANDBOX_MEMORY_MIB", default=4_096),
                pids=_positive_int(environment, "SANDBOX_PIDS", default=256),
            ),
            process_output_max_bytes=_positive_int(
                environment,
                "PROCESS_OUTPUT_MAX_BYTES",
                default=PROCESS_OUTPUT_MAX_BYTES,
            ),
            candidate_output_max_bytes=_positive_int(
                environment,
                "CANDIDATE_OUTPUT_MAX_BYTES",
                default=CANDIDATE_OUTPUT_MAX_BYTES,
            ),
            sandbox_cleanup_timeout_seconds=_positive_float(
                environment,
                "SANDBOX_CLEANUP_TIMEOUT_SECONDS",
                default=DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS,
            ),
            sandbox_name_prefix=sandbox_name_prefix,
        )
