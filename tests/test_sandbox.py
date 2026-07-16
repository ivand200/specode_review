import subprocess
from pathlib import Path

import pytest

from review_agent import SandboxResourceLimits
from review_agent.sandbox import DockerSandboxClient, DockerSandboxConfig, ProcessOptions


class RecordingProcessRunner:
    def __init__(self, *, stdout: bytes = b"") -> None:
        self.stdout = stdout
        self.calls: list[tuple[tuple[str, ...], ProcessOptions]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=self.stdout, stderr=b"")


def test_docker_sandbox_client_creates_a_bounded_isolated_mount_set(tmp_path: Path) -> None:
    process_runner = RecordingProcessRunner()
    client = DockerSandboxClient(
        executable=Path("/opt/review-agent/bin/sbx"),
        process_runner=process_runner,
        environment={},
        config=DockerSandboxConfig(process_output_max_bytes=4_096),
    )
    control = tmp_path / "control"
    checkout = tmp_path / "checkout"

    client.create(
        name="review-agent-" + "a" * 32,
        control=control,
        checkout=checkout,
        resources=SandboxResourceLimits(cpus=3, memory_mib=2_048, pids=64),
    )

    assert process_runner.calls == [
        (
            (
                "/opt/review-agent/bin/sbx",
                "create",
                "--quiet",
                "--name",
                "review-agent-" + "a" * 32,
                "--cpus",
                "3",
                "--memory",
                "2048m",
                "shell",
                str(control),
                f"{checkout}:ro",
            ),
            ProcessOptions(output_max_bytes=4_096, stage="sandbox_create", env={}),
        ),
        (
            (
                "/opt/review-agent/bin/sbx",
                "policy",
                "deny",
                "network",
                "--sandbox",
                "review-agent-" + "a" * 32,
                "**",
            ),
            ProcessOptions(output_max_bytes=4_096, stage="sandbox_network_policy", env={}),
        ),
    ]


def test_docker_sandbox_client_executes_with_process_and_output_limits() -> None:
    process_runner = RecordingProcessRunner(stdout=b"copied-head\n")
    client = DockerSandboxClient(
        executable=Path("/opt/review-agent/bin/sbx"),
        process_runner=process_runner,
        environment={},
        config=DockerSandboxConfig(process_output_max_bytes=8_192),
    )

    output = client.execute(
        name="review-agent-" + "b" * 32,
        command=("git", "rev-parse", "HEAD"),
        workdir="/home/agent/review/repo",
        process_limit=72,
    )

    assert output == b"copied-head\n"
    assert process_runner.calls == [
        (
            (
                "/opt/review-agent/bin/sbx",
                "exec",
                "--workdir",
                "/home/agent/review/repo",
                "review-agent-" + "b" * 32,
                "prlimit",
                "--nproc=72",
                "--",
                "git",
                "rev-parse",
                "HEAD",
            ),
            ProcessOptions(output_max_bytes=8_192, stage="sandbox_execute", env={}),
        )
    ]


def test_docker_sandbox_client_lists_names_and_forces_bounded_removal() -> None:
    process_runner = RecordingProcessRunner(
        stdout=b"review-agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nunrelated\n"
    )
    client = DockerSandboxClient(
        executable=Path("/opt/review-agent/bin/sbx"),
        process_runner=process_runner,
        environment={},
        config=DockerSandboxConfig(
            process_output_max_bytes=16_384,
            cleanup_timeout_seconds=7,
        ),
    )

    names = client.list_names()
    client.remove("review-agent-" + "a" * 32)

    assert names == (
        "review-agent-" + "a" * 32,
        "unrelated",
    )
    assert process_runner.calls == [
        (
            ("/opt/review-agent/bin/sbx", "ls", "--quiet"),
            ProcessOptions(output_max_bytes=16_384, stage="sandbox_list", env={}),
        ),
        (
            (
                "/opt/review-agent/bin/sbx",
                "rm",
                "--force",
                "review-agent-" + "a" * 32,
            ),
            ProcessOptions(
                output_max_bytes=16_384,
                stage="sandbox_cleanup",
                timeout_seconds=7,
                use_review_deadline=False,
                env={},
            ),
        ),
    ]


def test_docker_sandbox_client_does_not_forward_raw_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    process_runner = RecordingProcessRunner()
    client = DockerSandboxClient(
        executable=Path("/opt/review-agent/bin/sbx"),
        process_runner=process_runner,
    )

    client.create(
        name="review-agent-" + "c" * 32,
        control=tmp_path / "control",
        checkout=tmp_path / "checkout",
        resources=SandboxResourceLimits(),
    )

    for _arguments, options in process_runner.calls:
        assert options.env is not None
        assert "GITHUB_TOKEN" not in options.env
        assert "OPENAI_API_KEY" not in options.env
        assert "github-secret" not in options.env.values()
        assert "openai-secret" not in options.env.values()
