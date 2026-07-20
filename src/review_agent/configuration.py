import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

DEFAULT_SANDBOX_NAME_PREFIX = "review-agent-"
DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS = 30.0
DEFAULT_REVIEW_TIMEOUT_SECONDS = 15 * 60
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 1.0
DEFAULT_SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS = 30.0
CANDIDATE_OUTPUT_MAX_BYTES = 65_536
PROCESS_OUTPUT_MAX_BYTES = 1_048_576
PINNED_SBX_VERSION = "0.35.0"
PINNED_CODEX_VERSION = "0.144.6"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SANDBOX_PREFIX = re.compile(r"^[a-z0-9][a-z0-9.-]{1,29}-$")
_WEBHOOK_SECRET_MIN_CHARS = 32
_WEBHOOK_SECRET_MAX_CHARS = 1_024
_CODEX_MODEL_MAX_CHARS = 128
_MAX_CONCURRENT_REVIEWS = 10
_EXECUTOR_OS_ENVIRONMENT = frozenset(
    {
        "CURL_CA_BUNDLE",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


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


class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


@dataclass(frozen=True, slots=True)
class CodexExecutionPolicy:
    model: str
    reasoning_effort: ReasoningEffort

    def __post_init__(self) -> None:
        if (
            not self.model
            or len(self.model) > _CODEX_MODEL_MAX_CHARS
            or self.model.strip() != self.model
        ):
            message = "Codex model must be a non-empty bounded value"
            raise ValueError(message)
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            message = "Codex reasoning effort must be a ReasoningEffort"
            raise TypeError(message)


@dataclass(frozen=True, slots=True)
class SandboxOperationPolicy:
    process_output_max_bytes: int = PROCESS_OUTPUT_MAX_BYTES
    cleanup_timeout_seconds: float = DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS
    deny_network: bool = True

    def __post_init__(self) -> None:
        if self.process_output_max_bytes <= 0 or self.cleanup_timeout_seconds <= 0:
            message = "sandbox process limits must be positive"
            raise ValueError(message)


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
class WebhookSettings:
    repository: str
    secret: str = field(repr=False)
    max_concurrent_reviews: int


@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    periodic_interval_seconds: float
    shutdown_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PersistentStateSettings:
    root: Path
    repository_root: Path

    def __post_init__(self) -> None:
        expected_parent = self.root / "repositories"
        repository_directory = re.fullmatch(
            r"repository-v1-[0-9a-f]{64}",
            self.repository_root.name,
        )
        if (
            not self.root.is_absolute()
            or self.root == Path(self.root.anchor)
            or self.repository_root.parent != expected_parent
            or repository_directory is None
        ):
            message = "persistent repository state path is invalid"
            raise ValueError(message)

    @classmethod
    def for_repository(cls, *, root: Path, repository: str) -> "PersistentStateSettings":
        normalized_repository = repository.casefold()
        repository_digest = hashlib.sha256(normalized_repository.encode("utf-8")).hexdigest()
        return cls(
            root=root,
            repository_root=root / "repositories" / f"repository-v1-{repository_digest}",
        )


@dataclass(frozen=True, slots=True)
class AttemptSettings:
    app_id: int
    private_key_path: Path
    review_kit_path: Path
    workspace_root: Path
    codex_execution: CodexExecutionPolicy
    sandbox_resources: SandboxResourceLimits
    process_output_max_bytes: int
    candidate_output_max_bytes: int
    review_timeout_seconds: float
    sandbox_cleanup_timeout_seconds: float
    sandbox_name_prefix: str

    def __post_init__(self) -> None:
        if (
            self.process_output_max_bytes <= 0
            or self.candidate_output_max_bytes <= 0
            or self.review_timeout_seconds <= 0
            or self.sandbox_cleanup_timeout_seconds <= 0
        ):
            message = "attempt runtime limits must be positive"
            raise ValueError(message)
        if _SANDBOX_PREFIX.fullmatch(self.sandbox_name_prefix) is None:
            message = "sandbox name prefix is invalid"
            raise ValueError(message)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "AttemptSettings":
        codex_model = _required(environment, "CODEX_MODEL")
        if len(codex_model) > _CODEX_MODEL_MAX_CHARS or codex_model.strip() != codex_model:
            _invalid("CODEX_MODEL")

        try:
            reasoning_effort = ReasoningEffort(_required(environment, "OPENAI_REASONING_EFFORT"))
        except ValueError:
            _invalid("OPENAI_REASONING_EFFORT")

        workspace_root = _absolute_path(environment, "WORKSPACE_ROOT")
        if workspace_root == Path(workspace_root.anchor) or workspace_root.is_symlink():
            _invalid("WORKSPACE_ROOT")

        sandbox_name_prefix = environment.get(
            "SANDBOX_NAME_PREFIX",
            DEFAULT_SANDBOX_NAME_PREFIX,
        )
        if _SANDBOX_PREFIX.fullmatch(sandbox_name_prefix) is None:
            _invalid("SANDBOX_NAME_PREFIX")

        process_output_max_bytes = _positive_int(
            environment,
            "PROCESS_OUTPUT_MAX_BYTES",
            default=PROCESS_OUTPUT_MAX_BYTES,
        )
        return cls(
            app_id=_positive_int(environment, "GITHUB_APP_ID"),
            private_key_path=_existing_file(environment, "GITHUB_PRIVATE_KEY_PATH"),
            review_kit_path=_existing_directory(environment, "REVIEW_KIT_PATH"),
            workspace_root=workspace_root,
            codex_execution=CodexExecutionPolicy(
                model=codex_model,
                reasoning_effort=reasoning_effort,
            ),
            sandbox_resources=SandboxResourceLimits(
                cpus=_positive_int(environment, "SANDBOX_CPUS", default=2),
                memory_mib=_positive_int(
                    environment,
                    "SANDBOX_MEMORY_MIB",
                    default=4_096,
                ),
                pids=_positive_int(environment, "SANDBOX_PIDS", default=256),
            ),
            process_output_max_bytes=process_output_max_bytes,
            candidate_output_max_bytes=_positive_int(
                environment,
                "CANDIDATE_OUTPUT_MAX_BYTES",
                default=CANDIDATE_OUTPUT_MAX_BYTES,
            ),
            review_timeout_seconds=_positive_float(
                environment,
                "REVIEW_TIMEOUT_SECONDS",
                default=DEFAULT_REVIEW_TIMEOUT_SECONDS,
            ),
            sandbox_cleanup_timeout_seconds=_positive_float(
                environment,
                "SANDBOX_CLEANUP_TIMEOUT_SECONDS",
                default=DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS,
            ),
            sandbox_name_prefix=sandbox_name_prefix,
        )

    def render_executor_environment(
        self,
        parent_environment: Mapping[str, str],
    ) -> dict[str, str]:
        resources = self.sandbox_resources
        rendered = {
            name: value
            for name, value in parent_environment.items()
            if name in _EXECUTOR_OS_ENVIRONMENT or name.startswith("DOCKER_SANDBOXES_")
        }
        rendered.update(
            {
                "GITHUB_APP_ID": str(self.app_id),
                "GITHUB_PRIVATE_KEY_PATH": str(self.private_key_path),
                "CODEX_MODEL": self.codex_execution.model,
                "OPENAI_REASONING_EFFORT": self.codex_execution.reasoning_effort.value,
                "REVIEW_KIT_PATH": str(self.review_kit_path),
                "WORKSPACE_ROOT": str(self.workspace_root),
                "REVIEW_TIMEOUT_SECONDS": str(self.review_timeout_seconds),
                "SANDBOX_CPUS": str(resources.cpus),
                "SANDBOX_MEMORY_MIB": str(resources.memory_mib),
                "SANDBOX_PIDS": str(resources.pids),
                "PROCESS_OUTPUT_MAX_BYTES": str(self.process_output_max_bytes),
                "CANDIDATE_OUTPUT_MAX_BYTES": str(self.candidate_output_max_bytes),
                "SANDBOX_CLEANUP_TIMEOUT_SECONDS": str(
                    self.sandbox_cleanup_timeout_seconds
                ),
                "SANDBOX_NAME_PREFIX": self.sandbox_name_prefix,
            }
        )
        return rendered


@dataclass(frozen=True, slots=True)
class ProductionSettings:
    webhook: WebhookSettings
    attempt: AttemptSettings
    state: PersistentStateSettings
    reconciliation: ReconciliationSettings

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "ProductionSettings":
        repository = _required(environment, "GITHUB_REPOSITORY")
        if _REPOSITORY.fullmatch(repository) is None or repository.endswith(".git"):
            _invalid("GITHUB_REPOSITORY")

        webhook_secret = _required(environment, "GITHUB_WEBHOOK_SECRET")
        if not _WEBHOOK_SECRET_MIN_CHARS <= len(webhook_secret) <= _WEBHOOK_SECRET_MAX_CHARS:
            _invalid("GITHUB_WEBHOOK_SECRET")

        max_concurrent_reviews = _positive_int(
            environment,
            "MAX_CONCURRENT_REVIEWS",
            default=1,
        )
        if max_concurrent_reviews > _MAX_CONCURRENT_REVIEWS:
            _invalid("MAX_CONCURRENT_REVIEWS")

        attempt = AttemptSettings.from_environment(environment)
        state_root = _absolute_path(environment, "STATE_ROOT")
        if state_root == Path(state_root.anchor) or state_root.is_symlink():
            _invalid("STATE_ROOT")
        if state_root == attempt.workspace_root or state_root.is_relative_to(
            attempt.workspace_root
        ) or attempt.workspace_root.is_relative_to(state_root):
            _invalid("STATE_ROOT")

        return cls(
            webhook=WebhookSettings(
                repository=repository,
                secret=webhook_secret,
                max_concurrent_reviews=max_concurrent_reviews,
            ),
            attempt=attempt,
            state=PersistentStateSettings.for_repository(
                root=state_root,
                repository=repository,
            ),
            reconciliation=ReconciliationSettings(
                periodic_interval_seconds=_positive_float(
                    environment,
                    "RECONCILIATION_INTERVAL_SECONDS",
                    default=DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
                ),
                shutdown_timeout_seconds=_positive_float(
                    environment,
                    "SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS",
                    default=DEFAULT_SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS,
                ),
            ),
        )
