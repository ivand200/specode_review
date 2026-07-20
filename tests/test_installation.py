from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from specode_review.installation import InstallationError, NativeHostInstaller, main

_TEST_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65_537,
    key_size=2_048,
).private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)


def _production_environment() -> bytes:
    return b"\n".join(
        (
            b"GITHUB_APP_ID=1234",
            b"GITHUB_WEBHOOK_SECRET=installer-test-secret-value-123456789",
            b"PUBLIC_WEBHOOK_URL=https://reviews.example/webhooks/github",
            b"CODEX_MODEL=gpt-5.4",
            b"OPENAI_REASONING_EFFORT=high",
            b"MAX_CONCURRENT_REVIEWS=3",
            b"LOG_LEVEL=INFO",
        )
    )


@dataclass
class ControlledHost:
    commands: list[tuple[str, ...]] = field(default_factory=list)
    checked_out_tag: str = "v0.1.0"
    prepared: bool = False
    users: dict[str, tuple[Path, str]] = field(default_factory=dict)
    directories: dict[Path, tuple[str, str, int]] = field(default_factory=dict)
    files: dict[Path, bytes] = field(
        default_factory=lambda: {
            Path("/opt/specode-review/.env"): _production_environment(),
            Path("/opt/specode-review/.secrets/github-app.pem"): _TEST_PRIVATE_KEY,
        }
    )
    file_metadata: dict[Path, tuple[str, str, int]] = field(default_factory=dict)

    def run(self, *arguments: str) -> str:  # noqa: PLR0911 - controlled command adapter.
        self.commands.append(arguments)
        if arguments == ("git", "describe", "--tags", "--exact-match", "HEAD"):
            return f"{self.checked_out_tag}\n"
        if arguments[-2:] == ("sbx", "version"):
            return "sbx version: v0.35.0\n"
        if arguments[-2:] == ("codex", "--version"):
            return "codex-cli 0.144.6\n"
        if arguments == ("git", "--version"):
            return "git version 2.50.1\n"
        if "sbx" in arguments and "exec" in arguments:
            return "probe-ok"
        if arguments[-3:] == ("sbx", "ls", "--quiet"):
            return "specode-review-install-probe\n"
        return ""

    def require_supported_host(self) -> None:
        self.prepared = True

    def ensure_user(self, name: str, *, home: Path, login_shell: str) -> None:
        self.users[name] = (home, login_shell)

    def ensure_directory(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
    ) -> None:
        self.directories[path] = (owner, group, mode)

    def protect_file(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
        max_bytes: int,
    ) -> bytes:
        content = self.files[path]
        assert len(content) <= max_bytes
        self.file_metadata[path] = (owner, group, mode)
        return content

    def write_file(
        self,
        path: Path,
        content: bytes,
        *,
        owner: str,
        group: str,
        mode: int,
    ) -> None:
        self.files[path] = content
        self.file_metadata[path] = (owner, group, mode)

    def remove_tree(self, path: Path) -> None:
        for directory in tuple(self.directories):
            if directory == path or path in directory.parents:
                del self.directories[directory]
        for file_path in tuple(self.files):
            if file_path == path or path in file_path.parents:
                del self.files[file_path]
                self.file_metadata.pop(file_path, None)


def test_installer_rejects_anything_other_than_the_exact_supported_release_tag() -> None:
    host = ControlledHost()
    installer = NativeHostInstaller(host=host)

    for release in ("main", "0.1.0", "v0.1", "v0.1.1"):
        with pytest.raises(InstallationError, match="release_tag") as failure:
            installer.install(release)
        assert release not in str(failure.value)

    assert host.commands == []


def test_installer_rejects_a_checkout_that_is_not_at_the_requested_exact_tag() -> None:
    host = ControlledHost(checked_out_tag="v0.1.0-1-gabc123")
    installer = NativeHostInstaller(host=host)

    with pytest.raises(InstallationError, match="exact_release"):
        installer.install("v0.1.0")

    assert host.commands == [("git", "describe", "--tags", "--exact-match", "HEAD")]


def test_installer_provisions_the_dedicated_identity_and_narrow_directories() -> None:
    host = ControlledHost()

    NativeHostInstaller(host=host).install("v0.1.0")

    assert host.prepared is True
    assert host.users == {"specode-review": (Path("/var/lib/specode-review"), "/usr/sbin/nologin")}
    assert host.directories == {
        Path("/opt/specode-review"): ("root", "specode-review", 0o750),
        Path("/opt/specode-review/.secrets"): ("root", "specode-review", 0o750),
        Path("/var/lib/specode-review"): (
            "specode-review",
            "specode-review",
            0o750,
        ),
        Path("/var/lib/specode-review/workspaces"): (
            "specode-review",
            "specode-review",
            0o700,
        ),
        Path("/etc/systemd/system"): ("root", "root", 0o755),
    }
    assert host.file_metadata == {
        Path("/opt/specode-review/.env"): ("root", "specode-review", 0o640),
        Path("/opt/specode-review/.secrets/github-app.pem"): (
            "root",
            "specode-review",
            0o640,
        ),
        Path("/etc/systemd/system/specode-review.service"): ("root", "root", 0o644),
    }


def test_installer_generates_the_bounded_native_systemd_unit() -> None:
    host = ControlledHost()

    NativeHostInstaller(host=host).install("v0.1.0")

    unit = host.files[Path("/etc/systemd/system/specode-review.service")].decode()
    assert (
        unit
        == """[Unit]
Description=SpeCodeReview pull request review service
Wants=network-online.target docker.service
After=network-online.target docker.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=specode-review
Group=specode-review
WorkingDirectory=/opt/specode-review
Environment=HOME=/var/lib/specode-review
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
EnvironmentFile=/opt/specode-review/.env
ExecStart=/opt/specode-review/.venv/bin/specode-review
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20min
StandardOutput=journal
StandardError=journal
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
    )


def test_installer_capability_tests_pinned_tools_and_converges_idempotently() -> None:
    host = ControlledHost()
    installer = NativeHostInstaller(host=host)

    installer.install("v0.1.0")
    first_files = dict(host.files)
    first_metadata = dict(host.file_metadata)
    first_users = dict(host.users)
    first_directories = dict(host.directories)
    installer.install("v0.1.0")

    assert host.files == first_files
    assert host.file_metadata == first_metadata
    assert host.users == first_users
    assert host.directories == first_directories

    service_command = (
        "runuser",
        "-u",
        "specode-review",
        "--",
        "env",
        "-i",
        "HOME=/var/lib/specode-review",
        "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
    )

    def service(*arguments: str) -> tuple[str, ...]:
        return (*service_command, *arguments)

    expected_capabilities = (
        service("sbx", "version"),
        service("codex", "--version"),
        service("sbx", "diagnose"),
        service(
            "sbx",
            "create",
            "--quiet",
            "--name",
            "specode-review-install-probe",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "shell",
            "/var/lib/specode-review/install-probe/control",
            "/var/lib/specode-review/install-probe/readonly:ro",
        ),
        service(
            "sbx",
            "exec",
            "specode-review-install-probe",
            "sh",
            "-ceu",
            'test -w "$1"; test ! -w "$2"; printf probe-ok',
            "probe",
            "/var/lib/specode-review/install-probe/control",
            "/var/lib/specode-review/install-probe/readonly",
        ),
        service("sbx", "inspect", "specode-review-install-probe"),
        service("sbx", "policy", "ls", "specode-review-install-probe", "--json"),
        service("sbx", "secret", "ls", "specode-review-install-probe"),
        service("sbx", "ls", "--quiet"),
        service("sbx", "rm", "--force", "specode-review-install-probe"),
    )
    for command in expected_capabilities:
        assert host.commands.count(command) == 2
    assert host.commands.count(("uv", "sync", "--frozen", "--no-dev")) == 2
    assert host.commands.count(("systemctl", "daemon-reload")) == 2
    assert host.commands.count(("systemctl", "enable", "--now", "specode-review.service")) == 2


def test_installer_command_reports_only_a_bounded_safe_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "do-not-print-this-private-value"
    host = ControlledHost()
    host.files[Path("/opt/specode-review/.env")] = (
        _production_environment() + f"\nGITHUB_WEBHOOK_SECRET={sensitive_value}\n".encode()
    )

    exit_code = main(["--release", "v0.1.0"], host=host)

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.out == ""
    assert output.err == "installation failed: environment_file\n"
    assert sensitive_value not in output.err


def test_installer_rejects_a_malformed_github_app_key_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = b"-----BEGIN PRIVATE KEY-----\nprivate-sentinel\n-----END PRIVATE KEY-----\n"
    host = ControlledHost()
    host.files[Path("/opt/specode-review/.secrets/github-app.pem")] = sensitive_value

    exit_code = main(["--release", "v0.1.0"], host=host)

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.err == "installation failed: github_app_key\n"
    assert sensitive_value.decode() not in output.err


def test_installer_rejects_model_credentials_from_the_application_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_value = "sk-application-environment-sentinel"
    host = ControlledHost()
    host.files[Path("/opt/specode-review/.env")] = (
        _production_environment() + f"\nOPENAI_API_KEY={sensitive_value}\n".encode()
    )

    exit_code = main(["--release", "v0.1.0"], host=host)

    output = capsys.readouterr()
    assert exit_code == 1
    assert output.err == "installation failed: environment_interface\n"
    assert sensitive_value not in output.err
