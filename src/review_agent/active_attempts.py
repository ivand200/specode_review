import json
import os
import re
import stat
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Literal, NoReturn, Protocol

from pydantic import BaseModel, ConfigDict, Field

from review_agent.attempt import AttemptId
from review_agent.github import ReviewIdentity

ACTIVE_ATTEMPT_DOCUMENT_MAX_BYTES = 4_096
ACTIVE_ATTEMPT_MAX_ENTRIES = 10

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_ENTRY_NAME = re.compile(r"^attempt-v1-([0-9a-f]{32})\.json$")
_TEMPORARY_NAME = re.compile(r"^\.attempt-v1-([0-9a-f]{32})\.[0-9a-f]{32}\.tmp$")


class ActiveAttempt(BaseModel):
    """Bounded durable identity for review work that may still be active."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ReviewIdentity
    attempt_id: AttemptId
    check_run_id: int | None = Field(default=None, gt=0, strict=True)


class _ActiveAttemptDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    attempt: ActiveAttempt


class ActiveAttemptStateError(RuntimeError):
    """A normalized active-attempt persistence failure."""

    def __init__(self) -> None:
        self.stage = "active_attempt_state"
        super().__init__("active attempt state unavailable")


class ActiveAttemptRegistry(Protocol):
    def load(self) -> tuple[ActiveAttempt, ...]: ...

    def prepare(self, attempt: ActiveAttempt) -> None: ...

    def bind(self, *, attempt_id: str, check_run_id: int) -> None: ...

    def finish(self, *, attempt_id: str) -> None: ...


class VolatileActiveAttemptRegistry:
    """Process-local adapter for non-production coordinator callers."""

    def __init__(self) -> None:
        self._records: dict[str, ActiveAttempt] = {}

    def load(self) -> tuple[ActiveAttempt, ...]:
        return tuple(self._records.values())

    def prepare(self, attempt: ActiveAttempt) -> None:
        self._records[attempt.attempt_id] = attempt

    def bind(self, *, attempt_id: str, check_run_id: int) -> None:
        attempt = self._records[attempt_id]
        self._records[attempt_id] = attempt.model_copy(update={"check_run_id": check_run_id})

    def finish(self, *, attempt_id: str) -> None:
        self._records.pop(attempt_id, None)


class FileActiveAttemptRegistry:
    """Atomic repository-scoped registry of work active across process loss."""

    def __init__(self, repository_root: Path, *, repository: str) -> None:
        self._root = repository_root / "active-attempts-v1"
        self._repository = repository.lower()

    def load(self) -> tuple[ActiveAttempt, ...]:
        try:
            self._prepare_directory()
            names = sorted(path.name for path in self._root.iterdir())
            if len(names) > ACTIVE_ATTEMPT_MAX_ENTRIES:
                _invalid_state()
            records: list[ActiveAttempt] = []
            for name in names:
                if _TEMPORARY_NAME.fullmatch(name):
                    self._remove_temporary(self._root / name)
                    continue
                match = _ENTRY_NAME.fullmatch(name)
                if match is None:
                    _invalid_state()
                attempt = self._read(self._root / name)
                if attempt.attempt_id != match.group(1):
                    _invalid_state()
                records.append(attempt)
            if len({record.identity.external_id for record in records}) != len(records):
                _invalid_state()
            return tuple(records)
        except (OSError, ValueError):
            raise ActiveAttemptStateError from None

    def prepare(self, attempt: ActiveAttempt) -> None:
        try:
            current = self.load()
            if len(current) >= ACTIVE_ATTEMPT_MAX_ENTRIES:
                _invalid_state()
            if any(
                record.attempt_id == attempt.attempt_id
                or record.identity.external_id == attempt.identity.external_id
                for record in current
            ):
                _invalid_state()
            self._persist(attempt)
        except ActiveAttemptStateError:
            raise
        except (OSError, ValueError):
            raise ActiveAttemptStateError from None

    def bind(self, *, attempt_id: str, check_run_id: int) -> None:
        try:
            current = self._read(self._path(attempt_id))
            if current.check_run_id not in (None, check_run_id):
                _invalid_state()
            self._persist(current.model_copy(update={"check_run_id": check_run_id}))
        except (OSError, ValueError):
            raise ActiveAttemptStateError from None

    def finish(self, *, attempt_id: str) -> None:
        path = self._path(attempt_id)
        try:
            if not path.exists():
                if path.is_symlink():
                    _invalid_state()
                return
            self._read(path)
            path.unlink()
            self._sync_directory()
        except (OSError, ValueError):
            raise ActiveAttemptStateError from None

    def _prepare_directory(self) -> None:
        parent = self._root.parent
        parent_status = parent.lstat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or stat.S_ISLNK(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) != _DIRECTORY_MODE
        ):
            raise ValueError
        if not self._root.exists():
            self._root.mkdir(mode=_DIRECTORY_MODE)
        root_status = self._root.lstat()
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or stat.S_ISLNK(root_status.st_mode)
            or root_status.st_uid != os.geteuid()
            or stat.S_IMODE(root_status.st_mode) != _DIRECTORY_MODE
            or not os.access(self._root, os.R_OK | os.W_OK | os.X_OK)
        ):
            raise ValueError

    def _persist(self, attempt: ActiveAttempt) -> None:
        document = json.dumps(
            _ActiveAttemptDocument(schema_version=1, attempt=attempt).model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if len(document) > ACTIVE_ATTEMPT_DOCUMENT_MAX_BYTES:
            raise ValueError
        destination = self._path(attempt.attempt_id)
        temporary = self._root / (
            f".attempt-v1-{attempt.attempt_id}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = -1
        try:
            self._validate_destination(destination)
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
            raise

    def _read(self, path: Path) -> ActiveAttempt:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            status = os.fstat(descriptor)
            _validate_private_file(status)
            document = _read_bounded(descriptor)
        finally:
            os.close(descriptor)
        parsed = _ActiveAttemptDocument.model_validate_json(document, strict=True)
        if parsed.attempt.identity.repository != self._repository:
            raise ValueError
        return parsed.attempt

    def _remove_temporary(self, path: Path) -> None:
        self._read_private_file(path)
        path.unlink()

    def _validate_destination(self, path: Path) -> None:
        if not path.exists():
            if path.is_symlink():
                raise ValueError
            return
        self._read_private_file(path)

    def _read_private_file(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            _validate_private_file(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def _sync_directory(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _path(self, attempt_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise ValueError
        return self._root / f"attempt-v1-{attempt_id}.json"


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
    remaining = ACTIVE_ATTEMPT_DOCUMENT_MAX_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    document = b"".join(chunks)
    if len(document) > ACTIVE_ATTEMPT_DOCUMENT_MAX_BYTES:
        raise ValueError
    return document


def _write_all(descriptor: int, document: bytes) -> None:
    view = memoryview(document)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError
        view = view[written:]
