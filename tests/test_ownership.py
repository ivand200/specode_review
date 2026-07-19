import os
import subprocess
import sys
from pathlib import Path

import pytest

from review_agent.configuration import PersistentStateSettings
from review_agent.ownership import RepositoryOwnership, RepositoryOwnershipError


def _settings(tmp_path: Path, repository: str = "octo-org/example") -> PersistentStateSettings:
    return PersistentStateSettings.for_repository(
        root=tmp_path / "state",
        repository=repository,
    )


def test_repository_ownership_creates_private_hashed_state_and_releases_lock(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    ownership = RepositoryOwnership.acquire(settings)

    assert settings.root.stat().st_mode & 0o777 == 0o700
    assert settings.repository_root.parent.name == "repositories"
    assert settings.repository_root.name.startswith("repository-v1-")
    assert "octo-org" not in str(settings.repository_root)
    assert (settings.repository_root / "repository.lock").stat().st_mode & 0o777 == 0o600
    with pytest.raises(RepositoryOwnershipError, match="repository_lock"):
        RepositoryOwnership.acquire(settings)

    ownership.close()
    replacement = RepositoryOwnership.acquire(settings)
    replacement.close()


def test_repository_ownership_rejects_unsafe_state_paths(tmp_path: Path) -> None:
    linked_root = tmp_path / "linked-state"
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    linked_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RepositoryOwnershipError, match="state_root"):
        RepositoryOwnership.acquire(
            PersistentStateSettings.for_repository(
                root=linked_root,
                repository="octo-org/example",
            )
        )

    permissive_root = tmp_path / "permissive-state"
    permissive_root.mkdir(mode=0o755)
    with pytest.raises(RepositoryOwnershipError, match="state_root"):
        RepositoryOwnership.acquire(
            PersistentStateSettings.for_repository(
                root=permissive_root,
                repository="octo-org/example",
            )
        )


def test_distinct_repositories_can_hold_independent_locks(tmp_path: Path) -> None:
    first = RepositoryOwnership.acquire(_settings(tmp_path, "octo-org/first"))
    second = RepositoryOwnership.acquire(_settings(tmp_path, "octo-org/second"))

    first.close()
    second.close()


def test_real_process_contender_cannot_claim_the_same_repository(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    script = """
import sys
import types
from pathlib import Path

package = types.ModuleType("review_agent")
package.__path__ = [sys.argv[1]]
sys.modules["review_agent"] = package

from review_agent.configuration import PersistentStateSettings
from review_agent.ownership import RepositoryOwnership

settings = PersistentStateSettings.for_repository(root=Path(sys.argv[2]), repository=sys.argv[3])
ownership = RepositoryOwnership.acquire(settings)
print("locked", flush=True)
sys.stdin.buffer.read(1)
ownership.close()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(Path("src/review_agent").resolve()),
            str(settings.root),
            "octo-org/example",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"locked\n"
        with pytest.raises(RepositoryOwnershipError, match="repository_lock"):
            RepositoryOwnership.acquire(settings)
    finally:
        _, stderr = process.communicate(input=b"x", timeout=5)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")

    ownership = RepositoryOwnership.acquire(settings)
    ownership.close()
