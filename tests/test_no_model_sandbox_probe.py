import subprocess
from pathlib import Path

from specode_review import SandboxResourceLimits
from specode_review.configuration import SandboxOperationPolicy
from specode_review.process import ProcessOptions
from specode_review.resources import AttemptResources
from tests.integration.no_model_sandbox_probe import NoModelDockerSandboxProbe


class RecordingProcessRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], ProcessOptions]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")


def test_test_only_probe_creates_a_bounded_network_denied_shell_sandbox(
    tmp_path: Path,
) -> None:
    process_runner = RecordingProcessRunner()
    probe = NoModelDockerSandboxProbe(
        executable=Path("/opt/specode-review/bin/sbx"),
        process_runner=process_runner,
        environment={},
        config=SandboxOperationPolicy(
            process_output_max_bytes=4_096,
            cleanup_timeout_seconds=7,
        ),
    )
    resources = AttemptResources.for_attempt(
        "a" * 32,
        workspace_root=tmp_path / "workspaces",
        sandbox_prefix="specode-review-it-",
    )
    control = tmp_path / "control"
    checkout = tmp_path / "checkout"

    probe.create_stale(
        resources=resources,
        control=control,
        checkout=checkout,
        limits=SandboxResourceLimits(cpus=3, memory_mib=2_048, pids=64),
    )

    bounded_options = {
        "output_max_bytes": 4_096,
        "timeout_seconds": 7,
        "env": {},
    }
    assert process_runner.calls == [
        (
            (
                "/opt/specode-review/bin/sbx",
                "create",
                "--quiet",
                "--name",
                resources.sandbox_name,
                "--cpus",
                "3",
                "--memory",
                "2048m",
                "shell",
                str(control),
                f"{checkout}:ro",
            ),
            ProcessOptions(stage="sandbox_create", **bounded_options),
        ),
        (
            (
                "/opt/specode-review/bin/sbx",
                "policy",
                "deny",
                "network",
                "--sandbox",
                resources.sandbox_name,
                "**",
            ),
            ProcessOptions(stage="sandbox_network_policy", **bounded_options),
        ),
    ]
