from dataclasses import dataclass, field
from pathlib import Path

import pytest

import specode_review.resources
from specode_review.resources import AttemptResources, ReviewResourceManager


@dataclass
class RecordingSandboxResources:
    existing: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def list_names(self) -> tuple[str, ...]:
        return tuple(self.existing)

    def remove(self, name: str) -> None:
        self.existing.remove(name)
        self.removed.append(name)


def test_attempt_resources_use_one_validated_identity_for_exact_names(tmp_path: Path) -> None:
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=RecordingSandboxResources(),
    )

    resources = manager.for_attempt("a" * 32)

    assert resources.attempt_id == "a" * 32
    assert resources.workspace == tmp_path / "workspaces" / ("specode-review-workspace-" + "a" * 32)
    assert resources.sandbox_name == "specode-review-" + "a" * 32


def test_exact_cleanup_is_idempotent_and_preserves_other_attempts(tmp_path: Path) -> None:
    exact_id = "a" * 32
    other_id = "b" * 32
    exact_sandbox = "specode-review-" + exact_id
    other_sandbox = "specode-review-" + other_id
    client = RecordingSandboxResources(existing=[exact_sandbox, other_sandbox])
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=client,
    )
    exact = manager.for_attempt(exact_id)
    other = manager.for_attempt(other_id)
    exact.workspace.mkdir(parents=True)
    other.workspace.mkdir()

    manager.cleanup(exact_id)
    manager.cleanup(exact_id)

    assert not exact.workspace.exists()
    assert other.workspace.is_dir()
    assert client.removed == [exact_sandbox]
    assert client.existing == [other_sandbox]


def test_exact_cleanup_removes_the_sandbox_when_workspace_deletion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "a" * 32
    sandbox_name = "specode-review-" + attempt_id
    client = RecordingSandboxResources(existing=[sandbox_name])
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=client,
    )
    workspace = manager.for_attempt(attempt_id).workspace
    workspace.mkdir(parents=True)

    def fail_workspace_deletion(path: Path) -> None:
        assert path == workspace
        message = "simulated workspace deletion failure"
        raise OSError(message)

    monkeypatch.setattr(specode_review.resources.shutil, "rmtree", fail_workspace_deletion)

    with pytest.raises(OSError, match="simulated workspace deletion failure"):
        manager.cleanup(attempt_id)

    assert workspace.is_dir()
    assert client.removed == [sandbox_name]


def test_exact_cleanup_rejects_an_owned_name_that_is_a_symlink(tmp_path: Path) -> None:
    attempt_id = "a" * 32
    client = RecordingSandboxResources(existing=["specode-review-" + attempt_id])
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=client,
    )
    resources = manager.for_attempt(attempt_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    resources.workspace.parent.mkdir()
    resources.workspace.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="workspace"):
        manager.cleanup(attempt_id)

    assert resources.workspace.is_symlink()
    assert outside.is_dir()
    assert client.removed == []


def test_exact_cleanup_rejects_a_malformed_attempt_identity(tmp_path: Path) -> None:
    client = RecordingSandboxResources(existing=["specode-review-" + "a" * 32])
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=client,
    )

    with pytest.raises(ValueError, match="attempt identity"):
        manager.cleanup("../outside")

    assert client.removed == []


def test_attempt_resources_reject_root_and_outside_workspace_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workspace root"):
        AttemptResources.for_attempt(
            "a" * 32,
            workspace_root=Path("/"),
            sandbox_prefix="specode-review-",
        )

    with pytest.raises(ValueError, match="exact application-owned attempt path"):
        AttemptResources(
            attempt_id="a" * 32,
            workspace_root=tmp_path / "workspaces",
            workspace=tmp_path / "outside" / ("specode-review-workspace-" + "a" * 32),
            sandbox_prefix="specode-review-",
            sandbox_name="specode-review-" + "a" * 32,
        )

    with pytest.raises(ValueError, match="exact application-owned attempt name"):
        AttemptResources(
            attempt_id="a" * 32,
            workspace_root=tmp_path / "workspaces",
            workspace=tmp_path / "workspaces" / ("specode-review-workspace-" + "a" * 32),
            sandbox_prefix="specode-review-",
            sandbox_name="other-" + "a" * 32,
        )


def test_startup_sweep_removes_only_valid_stale_owned_resources(tmp_path: Path) -> None:
    stale_id = "a" * 32
    stale_sandbox = "specode-review-" + stale_id
    client = RecordingSandboxResources(
        existing=[
            stale_sandbox,
            "specode-review-not-an-attempt",
            "other-" + "c" * 32,
        ]
    )
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=client,
    )
    stale = manager.for_attempt(stale_id).workspace
    malformed = stale.parent / "specode-review-workspace-not-an-attempt"
    unrelated = stale.parent / ("other-" + "c" * 32)
    outside = tmp_path / "outside"
    stale.mkdir(parents=True)
    malformed.mkdir()
    unrelated.mkdir()
    outside.mkdir()

    manager.sweep_stale()

    assert not stale.exists()
    assert malformed.is_dir()
    assert unrelated.is_dir()
    assert outside.is_dir()
    assert client.removed == [stale_sandbox]


@pytest.mark.parametrize("entry_kind", ["symlink", "file"])
def test_startup_sweep_fails_closed_on_ambiguous_owned_workspace(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    manager = ReviewResourceManager(
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-",
        sandbox_client=RecordingSandboxResources(),
    )
    workspace = manager.for_attempt("b" * 32).workspace
    workspace.parent.mkdir()
    if entry_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        workspace.symlink_to(outside, target_is_directory=True)
    else:
        workspace.write_text("not a workspace", encoding="utf-8")

    with pytest.raises(ValueError, match="owned workspace"):
        manager.sweep_stale()

    assert workspace.exists() or workspace.is_symlink()
