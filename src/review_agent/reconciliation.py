import asyncio
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from types import TracebackType
from typing import Literal, NoReturn, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from review_agent.attempt import AttemptId, FailureStage
from review_agent.errors import ReviewError
from review_agent.github import (
    CheckRun,
    CheckRunConclusion,
    CheckRunOutputKind,
    CheckRunPresentation,
    CheckRunStatus,
    ExternalReviewId,
    GitHubError,
    GitHubOperation,
    ReviewIdentity,
    SafeOutputDetail,
    render_check_run_presentation,
)
from review_agent.models import RepositoryName, Sha

logger = logging.getLogger(__name__)

OUTBOX_DOCUMENT_MAX_BYTES = 16_384
OUTBOX_MAX_ENTRIES = 1_000
RECONCILIATION_RETRY_DELAYS_SECONDS = (1, 5, 30, 60, 300, 900)
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 1.0
DEFAULT_SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS = 30.0

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_ENTRY_NAME = re.compile(r"^check-run-v1-([1-9][0-9]*)\.json$")
_TEMPORARY_NAME = re.compile(r"^\.check-run-v1-([1-9][0-9]*)\.[0-9a-f]{32}\.tmp$")


class CheckRunUpdater(Protocol):
    def update_check_run(
        self,
        *,
        check_run_id: int,
        installation_id: int,
        presentation: CheckRunPresentation,
    ) -> CheckRun | None: ...


class ReconciliationStateError(RuntimeError):
    """A normalized persistent-state failure safe for readiness reporting."""

    def __init__(self, stage: str = "check_run_outbox") -> None:
        self.stage = stage
        super().__init__(f"check run reconciliation state unavailable: {stage}")


@dataclass(frozen=True, slots=True)
class DesiredCheckRun:
    check_run_id: int
    identity: ReviewIdentity
    attempt_id: AttemptId
    output_kind: CheckRunOutputKind
    finding_count: int | None = None
    failure_stage: FailureStage | None = None
    failure_category: SafeOutputDetail | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationTiming:
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    periodic_interval_seconds: float = DEFAULT_RECONCILIATION_INTERVAL_SECONDS
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.periodic_interval_seconds <= 0 or self.shutdown_timeout_seconds <= 0:
            message = "reconciliation timing values must be positive"
            raise ValueError(message)


class _DesiredCheckRunState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    check_run_id: int = Field(gt=0, strict=True)
    repository: RepositoryName
    pr_number: int = Field(gt=0, strict=True)
    base_sha: Sha
    head_sha: Sha
    external_id: ExternalReviewId
    desired_status: CheckRunStatus
    conclusion: CheckRunConclusion | None
    output_kind: CheckRunOutputKind
    finding_count: int | None = Field(default=None, ge=0, le=5, strict=True)
    failure_stage: FailureStage | None = None
    failure_category: SafeOutputDetail | None = None
    attempt_id: AttemptId
    updated_at: datetime
    generation: int = Field(gt=0, strict=True)

    @field_validator("repository", mode="before")
    @classmethod
    def normalize_repository(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("base_sha", "head_sha", mode="before")
    @classmethod
    def normalize_sha(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("updated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            message = "updated_at must be a UTC timestamp"
            raise ValueError(message)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def desired_fields_match_presentation(self) -> "_DesiredCheckRunState":
        presentation = self.presentation()
        if presentation.status is not self.desired_status:
            message = "desired status does not match output kind"
            raise ValueError(message)
        if presentation.conclusion is not self.conclusion:
            message = "conclusion does not match output kind"
            raise ValueError(message)
        return self

    def identity(self) -> ReviewIdentity:
        return ReviewIdentity(
            repository=self.repository,
            pr_number=self.pr_number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            external_id=self.external_id,
        )

    def presentation(self) -> CheckRunPresentation:
        return render_check_run_presentation(
            self.output_kind,
            identity=self.identity(),
            finding_count=self.finding_count,
            failure_stage=self.failure_stage,
            failure_category=self.failure_category,
        )


class _Lifecycle(Enum):
    CREATED = auto()
    ACTIVE = auto()
    STOPPING = auto()
    STOPPED = auto()


class _Outbox:
    def __init__(self, repository_root: Path, repository: str) -> None:
        self.root = repository_root / "check-run-outbox-v1"
        self._repository = repository.lower()

    def prepare_and_load(self) -> tuple[_DesiredCheckRunState, ...]:
        try:
            self._prepare_directory()
            entries: list[_DesiredCheckRunState] = []
            names = sorted(path.name for path in self.root.iterdir())
            if len(names) > OUTBOX_MAX_ENTRIES:
                _invalid_state()
            for name in names:
                if _TEMPORARY_NAME.fullmatch(name):
                    self._remove_stale_temporary(self.root / name)
                    continue
                match = _ENTRY_NAME.fullmatch(name)
                if match is None:
                    _invalid_state()
                state = self._read_path(self.root / name)
                if state.check_run_id != int(match.group(1)):
                    _invalid_state()
                entries.append(state)
            return tuple(entries)
        except (OSError, ValueError):
            raise ReconciliationStateError from None

    def load(self, check_run_id: int) -> _DesiredCheckRunState | None:
        path = self._path(check_run_id)
        try:
            if not path.exists():
                if path.is_symlink():
                    _invalid_state()
                return None
            return self._read_path(path)
        except (OSError, ValueError):
            raise ReconciliationStateError from None

    def persist(self, state: _DesiredCheckRunState) -> None:
        document = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(document) > OUTBOX_DOCUMENT_MAX_BYTES:
            raise ReconciliationStateError

        destination = self._path(state.check_run_id)
        temporary = self.root / (f".check-run-v1-{state.check_run_id}.{uuid.uuid4().hex}.tmp")
        descriptor = -1
        try:
            self._validate_existing_destination(destination)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                _FILE_MODE,
            )
            _write_all(descriptor, document)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            temporary.replace(destination)
            self._sync_directory()
        except (OSError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink()
            raise ReconciliationStateError from None

    def delete_if_generation(self, check_run_id: int, generation: int) -> bool:
        current = self.load(check_run_id)
        if current is None or current.generation != generation:
            return False
        try:
            self._path(check_run_id).unlink()
            self._sync_directory()
        except OSError:
            raise ReconciliationStateError from None
        return True

    def _prepare_directory(self) -> None:
        if not self.root.parent.is_dir() or self.root.parent.is_symlink():
            raise ValueError
        if not self.root.exists():
            self.root.mkdir(mode=_DIRECTORY_MODE)
        status = self.root.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE
            or not os.access(self.root, os.R_OK | os.W_OK | os.X_OK)
        ):
            raise ValueError

    def _read_path(self, path: Path) -> _DesiredCheckRunState:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            status = os.fstat(descriptor)
            _validate_private_file(status)
            document = _read_bounded(descriptor)
        finally:
            os.close(descriptor)
        state = _DesiredCheckRunState.model_validate_json(document, strict=True)
        if state.repository != self._repository:
            raise ValueError
        return state

    def _remove_stale_temporary(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            _validate_private_file(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        path.unlink()

    def _validate_existing_destination(self, path: Path) -> None:
        if not path.exists():
            if path.is_symlink():
                raise ValueError
            return
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            _validate_private_file(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def _sync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _path(self, check_run_id: int) -> Path:
        if check_run_id < 1:
            raise ValueError
        return self.root / f"check-run-v1-{check_run_id}.json"


class CheckRunReconciler:
    """Persists and delivers only the latest desired state for each Check Run."""

    def __init__(
        self,
        *,
        repository_root: Path,
        repository: str,
        installation_id: int,
        github: CheckRunUpdater,
        timing: ReconciliationTiming | None = None,
    ) -> None:
        if installation_id < 1:
            message = "installation_id must be positive"
            raise ValueError(message)
        resolved_timing = timing or ReconciliationTiming()
        self._repository = repository.lower()
        self._installation_id = installation_id
        self._github = github
        self._clock = resolved_timing.clock
        self._sleeper = resolved_timing.sleeper
        self._periodic_interval_seconds = resolved_timing.periodic_interval_seconds
        self._shutdown_timeout_seconds = resolved_timing.shutdown_timeout_seconds
        self._outbox = _Outbox(repository_root, repository)
        self._lifecycle = _Lifecycle.CREATED
        self._state_lock = asyncio.Lock()
        self._delivery_locks: dict[int, asyncio.Lock] = {}
        self._last_generation: dict[int, int] = {}
        self._failure_counts: dict[tuple[int, int], int] = {}
        self._next_attempt_at: dict[tuple[int, int], datetime] = {}
        self._periodic_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if self._lifecycle is not _Lifecycle.CREATED:
            message = "check run reconciler cannot be restarted"
            raise RuntimeError(message)
        states = self._outbox.prepare_and_load()
        self._lifecycle = _Lifecycle.ACTIVE
        for state in states:
            self._last_generation[state.check_run_id] = state.generation
            await self._reconcile(state, force=True)
        self._periodic_task = asyncio.create_task(self._periodic_reconciliation())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._lifecycle = _Lifecycle.STOPPING
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._periodic_task
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await self.reconcile_pending(force=True)
        except TimeoutError:
            logger.warning(
                "check run reconciliation timed out operation=%s repository=%s",
                GitHubOperation.CHECK_RUN_UPDATE.value,
                self._repository,
            )
        finally:
            self._lifecycle = _Lifecycle.STOPPED

    async def set_desired(self, desired: DesiredCheckRun) -> None:
        if self._lifecycle is not _Lifecycle.ACTIVE:
            message = "check run reconciler is not accepting desired states"
            raise RuntimeError(message)
        if desired.identity.repository != self._repository:
            message = "review identity does not match the configured repository"
            raise ValueError(message)
        presentation = render_check_run_presentation(
            desired.output_kind,
            identity=desired.identity,
            finding_count=desired.finding_count,
            failure_stage=desired.failure_stage,
            failure_category=desired.failure_category,
        )
        async with self._state_lock:
            current = self._outbox.load(desired.check_run_id)
            previous_generation = self._last_generation.get(desired.check_run_id, 0)
            if current is not None:
                previous_generation = max(previous_generation, current.generation)
            state = _DesiredCheckRunState(
                schema_version=1,
                check_run_id=desired.check_run_id,
                repository=desired.identity.repository,
                pr_number=desired.identity.pr_number,
                base_sha=desired.identity.base_sha,
                head_sha=desired.identity.head_sha,
                external_id=desired.identity.external_id,
                desired_status=presentation.status,
                conclusion=presentation.conclusion,
                output_kind=desired.output_kind,
                finding_count=desired.finding_count,
                failure_stage=desired.failure_stage,
                failure_category=desired.failure_category,
                attempt_id=desired.attempt_id,
                updated_at=self._now(),
                generation=previous_generation + 1,
            )
            self._outbox.persist(state)
            self._last_generation[desired.check_run_id] = state.generation
            self._clear_old_schedules(state)
        await self._reconcile(state, force=True)

    async def reconcile_pending(self, *, force: bool = False) -> None:
        states = self._outbox.prepare_and_load()
        for state in states:
            self._last_generation[state.check_run_id] = max(
                state.generation,
                self._last_generation.get(state.check_run_id, 0),
            )
            await self._reconcile(state, force=force)

    async def _reconcile(self, state: _DesiredCheckRunState, *, force: bool) -> None:
        key = (state.check_run_id, state.generation)
        next_attempt_at = self._next_attempt_at.get(key, datetime.min.replace(tzinfo=UTC))
        if not force and next_attempt_at > self._now():
            return
        delivery_lock = self._delivery_locks.setdefault(state.check_run_id, asyncio.Lock())
        async with delivery_lock:
            async with self._state_lock:
                current = self._outbox.load(state.check_run_id)
                if current is None or current.generation != state.generation:
                    return
            try:
                await asyncio.to_thread(
                    self._github.update_check_run,
                    check_run_id=state.check_run_id,
                    installation_id=self._installation_id,
                    presentation=state.presentation(),
                )
            except (GitHubError, ReviewError):
                self._record_failure(state)
                logger.warning(
                    "check run reconciliation failed operation=%s repository=%s "
                    "check_run_id=%d attempt_id=%s",
                    GitHubOperation.CHECK_RUN_UPDATE.value,
                    self._repository,
                    state.check_run_id,
                    state.attempt_id,
                )
                return
            async with self._state_lock:
                deleted = self._outbox.delete_if_generation(
                    state.check_run_id,
                    state.generation,
                )
                if deleted:
                    self._failure_counts.pop(key, None)
                    self._next_attempt_at.pop(key, None)

    def _record_failure(self, state: _DesiredCheckRunState) -> None:
        key = (state.check_run_id, state.generation)
        failure_count = self._failure_counts.get(key, 0) + 1
        self._failure_counts[key] = failure_count
        delay_index = min(failure_count - 1, len(RECONCILIATION_RETRY_DELAYS_SECONDS) - 1)
        delay = RECONCILIATION_RETRY_DELAYS_SECONDS[delay_index]
        self._next_attempt_at[key] = self._now() + timedelta(seconds=delay)

    def _clear_old_schedules(self, state: _DesiredCheckRunState) -> None:
        for key in tuple(self._failure_counts):
            if key[0] == state.check_run_id and key[1] != state.generation:
                self._failure_counts.pop(key, None)
                self._next_attempt_at.pop(key, None)

    async def _periodic_reconciliation(self) -> None:
        while self._lifecycle is _Lifecycle.ACTIVE:
            await self._sleeper(self._periodic_interval_seconds)
            if self._lifecycle is _Lifecycle.ACTIVE:
                try:
                    await self.reconcile_pending()
                except ReconciliationStateError:
                    logger.warning(
                        "check run reconciliation state failed operation=%s repository=%s",
                        GitHubOperation.CHECK_RUN_UPDATE.value,
                        self._repository,
                    )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            message = "reconciliation clock must return UTC timestamps"
            raise ValueError(message)
        return value.astimezone(UTC)


def _validate_private_file(status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != _FILE_MODE
    ):
        raise ValueError


def _invalid_state() -> NoReturn:
    raise ValueError


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = OUTBOX_DOCUMENT_MAX_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4_096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    document = b"".join(chunks)
    if not document or len(document) > OUTBOX_DOCUMENT_MAX_BYTES:
        raise ValueError
    return document


def _write_all(descriptor: int, document: bytes) -> None:
    written = 0
    while written < len(document):
        count = os.write(descriptor, document[written:])
        if count <= 0:
            raise OSError
        written += count
