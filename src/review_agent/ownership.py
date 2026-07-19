import fcntl
import os
import stat
from pathlib import Path
from typing import Self

from review_agent.configuration import PersistentStateSettings

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class RepositoryOwnershipError(RuntimeError):
    """A normalized host-ownership failure safe for startup reporting."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"repository ownership unavailable: {stage}")


class RepositoryOwnership:
    def __init__(self, *, lock_descriptor: int, repository_root: Path) -> None:
        self._lock_descriptor = lock_descriptor
        self.repository_root = repository_root

    @classmethod
    def acquire(cls, settings: PersistentStateSettings) -> Self:
        try:
            _prepare_private_directory(settings.root)
            repositories_root = settings.root / "repositories"
            _prepare_private_directory(repositories_root)
            _prepare_private_directory(settings.repository_root)
        except (OSError, ValueError):
            stage = "state_root"
            raise RepositoryOwnershipError(stage) from None

        lock_path = settings.repository_root / "repository.lock"
        descriptor = -1
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                _PRIVATE_FILE_MODE,
            )
            lock_status = os.fstat(descriptor)
            _validate_lock_file(lock_status)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            stage = "repository_lock"
            raise RepositoryOwnershipError(stage) from None
        return cls(lock_descriptor=descriptor, repository_root=settings.repository_root)

    def close(self) -> None:
        if self._lock_descriptor < 0:
            return
        descriptor = self._lock_descriptor
        self._lock_descriptor = -1
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


def _prepare_private_directory(path: Path) -> None:
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError
    missing: list[Path] = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError
        missing.append(current)
        current = current.parent
    _validate_directory_components(current, path)
    for directory in reversed(missing):
        directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != _PRIVATE_DIRECTORY_MODE
        or not os.access(path, os.R_OK | os.W_OK | os.X_OK)
    ):
        raise ValueError


def _validate_lock_file(lock_status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(lock_status.st_mode)
        or lock_status.st_uid != os.geteuid()
        or stat.S_IMODE(lock_status.st_mode) != _PRIVATE_FILE_MODE
    ):
        raise ValueError


def _validate_directory_components(existing: Path, destination: Path) -> None:
    current = Path(destination.anchor)
    for component in destination.parts[1:]:
        current /= component
        if not current.exists() and current != existing:
            continue
        status = current.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ValueError
