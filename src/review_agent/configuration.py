import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

DEFAULT_SANDBOX_NAME_PREFIX = "review-agent-"
DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS = 30.0
DEFAULT_REVIEW_TIMEOUT_SECONDS = 15 * 60
CANDIDATE_OUTPUT_MAX_BYTES = 65_536
PROCESS_OUTPUT_MAX_BYTES = 1_048_576
PINNED_SBX_VERSION = "0.35.0"
PINNED_CODEX_VERSION = "0.144.6"

_SANDBOX_PREFIX = re.compile(r"^[a-z0-9][a-z0-9.-]{1,29}-$")
_WEBHOOK_SECRET_MIN_CHARS = 32
_WEBHOOK_SECRET_MAX_CHARS = 1_024
_CODEX_MODEL_MAX_CHARS = 128
_MAX_SERVICE_CONCURRENT_REVIEWS = 5
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


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

@dataclass(frozen=True, slots=True)
class ProductionPaths:
    """Application-owned filesystem contract, not operator configuration."""

    private_key_path: Path = Path("/opt/review-agent/.secrets/github-app.pem")
    review_kit_path: Path = Path("/opt/review-agent/review-kit")
    workspace_root: Path = Path("/var/lib/review-agent/workspaces")
    sandbox_name_prefix: str = DEFAULT_SANDBOX_NAME_PREFIX

    def __post_init__(self) -> None:
        for path in (
            self.private_key_path,
            self.review_kit_path,
            self.workspace_root,
        ):
            if not path.is_absolute() or path == Path(path.anchor):
                message = "production paths must be absolute non-root paths"
                raise ValueError(message)
        if _SANDBOX_PREFIX.fullmatch(self.sandbox_name_prefix) is None:
            message = "sandbox name prefix is invalid"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ProductionServiceSettings:
    """Small operator surface for the in-process comment-only service."""

    app_id: int
    webhook_secret: str = field(repr=False)
    public_webhook_url: str
    codex_execution: CodexExecutionPolicy
    max_concurrent_reviews: int = 3
    log_level: str = "INFO"
    paths: ProductionPaths = field(default_factory=ProductionPaths)

    @property
    def attempt(self) -> AttemptSettings:
        return AttemptSettings(
            app_id=self.app_id,
            private_key_path=self.paths.private_key_path,
            review_kit_path=self.paths.review_kit_path,
            workspace_root=self.paths.workspace_root,
            codex_execution=self.codex_execution,
            sandbox_resources=SandboxResourceLimits(
                cpus=2,
                memory_mib=2_048,
                pids=256,
            ),
            process_output_max_bytes=PROCESS_OUTPUT_MAX_BYTES,
            candidate_output_max_bytes=CANDIDATE_OUTPUT_MAX_BYTES,
            review_timeout_seconds=DEFAULT_REVIEW_TIMEOUT_SECONDS,
            sandbox_cleanup_timeout_seconds=DEFAULT_SANDBOX_CLEANUP_TIMEOUT_SECONDS,
            sandbox_name_prefix=self.paths.sandbox_name_prefix,
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        paths: ProductionPaths | None = None,
    ) -> "ProductionServiceSettings":
        webhook_secret = _required(environment, "GITHUB_WEBHOOK_SECRET")
        if not _WEBHOOK_SECRET_MIN_CHARS <= len(webhook_secret) <= _WEBHOOK_SECRET_MAX_CHARS:
            _invalid("GITHUB_WEBHOOK_SECRET")

        public_webhook_url = _required(environment, "PUBLIC_WEBHOOK_URL")
        parsed_url = urlsplit(public_webhook_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.path != "/webhooks/github"
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            _invalid("PUBLIC_WEBHOOK_URL")

        model = _required(environment, "CODEX_MODEL")
        if len(model) > _CODEX_MODEL_MAX_CHARS or model.strip() != model:
            _invalid("CODEX_MODEL")
        try:
            reasoning = ReasoningEffort(_required(environment, "OPENAI_REASONING_EFFORT"))
        except ValueError:
            _invalid("OPENAI_REASONING_EFFORT")

        concurrency = _positive_int(
            environment,
            "MAX_CONCURRENT_REVIEWS",
            default=3,
        )
        if concurrency > _MAX_SERVICE_CONCURRENT_REVIEWS:
            _invalid("MAX_CONCURRENT_REVIEWS")

        log_level = environment.get("LOG_LEVEL", "INFO").upper()
        if log_level not in _LOG_LEVELS:
            _invalid("LOG_LEVEL")

        return cls(
            app_id=_positive_int(environment, "GITHUB_APP_ID"),
            webhook_secret=webhook_secret,
            public_webhook_url=public_webhook_url,
            codex_execution=CodexExecutionPolicy(
                model=model,
                reasoning_effort=reasoning,
            ),
            max_concurrent_reviews=concurrency,
            log_level=log_level,
            paths=paths or ProductionPaths(),
        )
