import argparse
import grp
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from specode_review.configuration import (
    PINNED_NGROK_VERSION,
    ConfigurationError,
    ProductionServiceSettings,
)
from specode_review.verification import WebhookMismatchError, system_verifier

SUPPORTED_RELEASE_TAG = "v0.1.0"
PINNED_SBX_VERSION = "0.35.0"
PINNED_CODEX_VERSION = "0.144.6"
_APPLICATION_ROOT = Path("/opt/specode-review")
_SERVICE_USER = "specode-review"
_SERVICE_HOME = Path("/var/lib/specode-review")
_PROBE_ROOT = _SERVICE_HOME / "install-probe"
_PROBE_NAME = "specode-review-install-probe"
_COMMAND_OUTPUT_MAX_BYTES = 1_048_576
_COMMAND_TIMEOUT_SECONDS = 300
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "GITHUB_APP_ID",
        "GITHUB_WEBHOOK_SECRET",
        "PUBLIC_WEBHOOK_URL",
        "NGROK_URL",
        "NGROK_AUTHTOKEN",
        "CODEX_MODEL",
        "OPENAI_REASONING_EFFORT",
        "MAX_CONCURRENT_REVIEWS",
        "LOG_LEVEL",
    }
)
_SYSTEMD_UNIT = b"""[Unit]
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
_NGROK_SYSTEMD_UNIT = b"""[Unit]
Description=SpeCodeReview reserved ngrok ingress
Wants=network-online.target specode-review.service
After=network-online.target specode-review.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=specode-review
Group=specode-review
Environment=HOME=/var/lib/specode-review
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
EnvironmentFile=/opt/specode-review/.env
ExecStart=/usr/bin/env ngrok http --url=${NGROK_URL} http://127.0.0.1:8000
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


class InstallationError(RuntimeError):
    """A bounded installer failure that is safe to print."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"installation failed: {stage}")


def _fail(stage: str) -> NoReturn:
    raise InstallationError(stage)


class InstallationHost(Protocol):
    def run(self, *arguments: str) -> str: ...

    def require_supported_host(self) -> None: ...

    def ensure_user(self, name: str, *, home: Path, login_shell: str) -> None: ...

    def ensure_directory(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
    ) -> None: ...

    def protect_file(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
        max_bytes: int,
    ) -> bytes: ...

    def write_file(
        self,
        path: Path,
        content: bytes,
        *,
        owner: str,
        group: str,
        mode: int,
    ) -> None: ...

    def remove_tree(self, path: Path) -> None: ...

    def verify_installation(self) -> None: ...


def _parse_environment(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        _fail("environment_file")
    environment: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or name.strip() != name:
            _fail("environment_file")
        if name in environment:
            _fail("environment_file")
        environment[name] = value
    return environment


class NativeHostInstaller:
    """Converge one native host on the supported SpeCodeReview release."""

    def __init__(self, *, host: InstallationHost) -> None:
        self._host = host

    @staticmethod
    def _service_command(*arguments: str) -> tuple[str, ...]:
        return (
            "runuser",
            "-u",
            _SERVICE_USER,
            "--",
            "env",
            "-i",
            f"HOME={_SERVICE_HOME}",
            f"PATH={_SAFE_PATH}",
            *arguments,
        )

    def _check_tools(self, *, managed_ngrok: bool) -> None:
        self._host.run("uv", "sync", "--frozen", "--no-dev")
        sbx_version = self._host.run(*self._service_command("sbx", "version")).strip()
        if sbx_version != f"sbx version: v{PINNED_SBX_VERSION}":
            _fail("sbx_version")
        codex_version = self._host.run(*self._service_command("codex", "--version")).strip()
        if codex_version != f"codex-cli {PINNED_CODEX_VERSION}":
            _fail("codex_version")
        self._host.run("git", "--version")
        self._host.run(*self._service_command("sbx", "diagnose"))
        if managed_ngrok:
            ngrok_version = self._host.run(
                *self._service_command("ngrok", "version")
            ).strip()
            if ngrok_version != f"ngrok version {PINNED_NGROK_VERSION}":
                _fail("ngrok_version")

    def _probe_sandbox_capabilities(self) -> None:
        control = _PROBE_ROOT / "control"
        read_only = _PROBE_ROOT / "readonly"
        for path in (control, read_only):
            self._host.ensure_directory(
                path,
                owner=_SERVICE_USER,
                group=_SERVICE_USER,
                mode=0o700,
            )
        self._host.write_file(
            read_only / "mount-marker",
            b"probe\n",
            owner=_SERVICE_USER,
            group=_SERVICE_USER,
            mode=0o600,
        )
        created = False
        try:
            self._host.run(
                *self._service_command(
                    "sbx",
                    "create",
                    "--quiet",
                    "--name",
                    _PROBE_NAME,
                    "--cpus",
                    "1",
                    "--memory",
                    "512m",
                    "shell",
                    str(control),
                    f"{read_only}:ro",
                )
            )
            created = True
            self._host.run(
                *self._service_command(
                    "sbx",
                    "policy",
                    "deny",
                    "network",
                    "--sandbox",
                    _PROBE_NAME,
                    "**",
                )
            )
            probe_output = self._host.run(
                *self._service_command(
                    "sbx",
                    "exec",
                    _PROBE_NAME,
                    "sh",
                    "-ceu",
                    'test -w "$1"; test ! -w "$2"; printf probe-ok',
                    "probe",
                    str(control),
                    str(read_only),
                )
            )
            if probe_output != "probe-ok":
                _fail("sandbox_mount_execute")
            self._host.run(*self._service_command("sbx", "inspect", _PROBE_NAME))
            self._host.run(
                *self._service_command(
                    "sbx",
                    "policy",
                    "ls",
                    _PROBE_NAME,
                    "--json",
                )
            )
            self._host.run(*self._service_command("sbx", "secret", "ls", _PROBE_NAME))
            sandbox_names = self._host.run(
                *self._service_command("sbx", "ls", "--quiet")
            ).splitlines()
            if _PROBE_NAME not in sandbox_names:
                _fail("sandbox_list")
        finally:
            if created:
                self._host.run(*self._service_command("sbx", "rm", "--force", _PROBE_NAME))
            self._host.remove_tree(_PROBE_ROOT)

    @staticmethod
    def _validated_environment(
        environment_content: bytes,
        private_key: bytes,
    ) -> dict[str, str]:
        try:
            parsed_key = serialization.load_pem_private_key(
                private_key,
                password=None,
            )
        except (TypeError, ValueError):
            _fail("github_app_key")
        if not isinstance(parsed_key, rsa.RSAPrivateKey):
            _fail("github_app_key")
        environment = _parse_environment(environment_content)
        if not environment.keys() <= _ALLOWED_ENVIRONMENT_NAMES:
            _fail("environment_interface")
        if any("replace-with" in value.lower() for value in environment.values()):
            _fail("environment_placeholder")
        if bool(environment.get("NGROK_URL")) != bool(environment.get("NGROK_AUTHTOKEN")):
            _fail("environment_ngrok")
        ngrok_url = environment.get("NGROK_URL")
        if ngrok_url:
            NativeHostInstaller._validate_ngrok_origin(
                ngrok_url,
                environment.get("PUBLIC_WEBHOOK_URL", ""),
            )
        try:
            ProductionServiceSettings.from_environment(environment)
        except ConfigurationError as error:
            _fail(f"configuration_{error.setting}")
        return environment

    @staticmethod
    def _validate_ngrok_origin(ngrok_url: str, public_webhook_url: str) -> None:
        parsed_ngrok = urlsplit(ngrok_url)
        parsed_public = urlsplit(public_webhook_url)
        if (
            parsed_ngrok.scheme != "https"
            or not parsed_ngrok.netloc
            or parsed_ngrok.path not in {"", "/"}
            or parsed_ngrok.query
            or parsed_ngrok.fragment
            or parsed_ngrok.username is not None
            or parsed_ngrok.password is not None
            or (parsed_ngrok.scheme, parsed_ngrok.netloc)
            != (parsed_public.scheme, parsed_public.netloc)
        ):
            _fail("environment_ngrok")

    def _write_units(self, *, managed_ngrok: bool) -> None:
        self._host.write_file(
            Path("/etc/systemd/system/specode-review.service"),
            _SYSTEMD_UNIT,
            owner="root",
            group="root",
            mode=0o644,
        )
        if managed_ngrok:
            self._host.write_file(
                Path("/etc/systemd/system/specode-review-ngrok.service"),
                _NGROK_SYSTEMD_UNIT,
                owner="root",
                group="root",
                mode=0o644,
            )

    def install(self, release_tag: str) -> None:
        if release_tag != SUPPORTED_RELEASE_TAG:
            _fail("release_tag")
        checked_out_tag = self._host.run(
            "git",
            "describe",
            "--tags",
            "--exact-match",
            "HEAD",
        ).strip()
        if checked_out_tag != release_tag:
            _fail("exact_release")
        self._host.require_supported_host()
        self._host.ensure_user(
            _SERVICE_USER,
            home=Path("/var/lib/specode-review"),
            login_shell="/usr/sbin/nologin",
        )
        for path, owner, group, mode in (
            (_APPLICATION_ROOT, "root", _SERVICE_USER, 0o750),
            (_APPLICATION_ROOT / ".secrets", "root", _SERVICE_USER, 0o750),
            (
                Path("/var/lib/specode-review"),
                _SERVICE_USER,
                _SERVICE_USER,
                0o750,
            ),
            (
                Path("/var/lib/specode-review/workspaces"),
                _SERVICE_USER,
                _SERVICE_USER,
                0o700,
            ),
            (Path("/etc/systemd/system"), "root", "root", 0o755),
        ):
            self._host.ensure_directory(
                path,
                owner=owner,
                group=group,
                mode=mode,
            )
        environment_content = self._host.protect_file(
            _APPLICATION_ROOT / ".env",
            owner="root",
            group=_SERVICE_USER,
            mode=0o640,
            max_bytes=65_536,
        )
        private_key = self._host.protect_file(
            _APPLICATION_ROOT / ".secrets/github-app.pem",
            owner="root",
            group=_SERVICE_USER,
            mode=0o640,
            max_bytes=65_536,
        )
        environment = self._validated_environment(environment_content, private_key)
        ngrok_url = environment.get("NGROK_URL")
        self._write_units(managed_ngrok=bool(ngrok_url))
        self._check_tools(managed_ngrok=bool(ngrok_url))
        self._probe_sandbox_capabilities()
        self._host.run("systemctl", "daemon-reload")
        self._host.run("systemctl", "enable", "--now", "specode-review.service")
        if ngrok_url:
            self._host.run(
                "systemctl",
                "enable",
                "--now",
                "specode-review-ngrok.service",
            )
        self._host.verify_installation()


class SystemInstallationHost:
    """Privileged Ubuntu adapter for the native-host installation seam."""

    def run(self, *arguments: str) -> str:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
            or name.startswith("DOCKER_SANDBOXES_")
        }
        environment["PATH"] = _SAFE_PATH
        try:
            completed = subprocess.run(  # noqa: S603 - fixed installer-owned commands.
                arguments,
                cwd=_APPLICATION_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            _fail("host_command")
        if (
            completed.returncode != 0
            or len(completed.stdout) > _COMMAND_OUTPUT_MAX_BYTES
            or len(completed.stderr) > _COMMAND_OUTPUT_MAX_BYTES
        ):
            _fail("host_command")
        return completed.stdout.decode("utf-8", errors="replace")

    def require_supported_host(self) -> None:
        if os.geteuid() != 0:
            _fail("root_required")
        try:
            os_release = Path("/etc/os-release").read_text(
                encoding="utf-8",
                errors="strict",
            )
        except (OSError, UnicodeError):
            _fail("host_platform")
        identity = {
            key: value.strip().strip('"')
            for line in os_release.splitlines()
            if (separator := line.find("=")) > 0
            for key, value in ((line[:separator], line[separator + 1 :]),)
        }
        if identity.get("ID") != "ubuntu" or not Path("/run/systemd/system").is_dir():
            _fail("host_platform")
        for executable in ("git", "runuser", "systemctl", "uv", "sbx", "codex"):
            if shutil.which(executable, path=_SAFE_PATH) is None:
                _fail(f"host_tool_{executable}")

    def ensure_user(self, name: str, *, home: Path, login_shell: str) -> None:
        try:
            service_group = grp.getgrnam(name)
        except KeyError:
            self.run("groupadd", "--system", name)
            try:
                service_group = grp.getgrnam(name)
            except KeyError:
                _fail("service_identity")
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            self.run(
                "useradd",
                "--system",
                "--gid",
                name,
                "--create-home",
                "--home-dir",
                str(home),
                "--shell",
                login_shell,
                name,
            )
            try:
                account = pwd.getpwnam(name)
            except KeyError:
                _fail("service_identity")
        repair_arguments: list[str] = ["usermod"]
        if account.pw_gid != service_group.gr_gid:
            repair_arguments.extend(("--gid", name))
        if account.pw_dir != str(home):
            repair_arguments.extend(("--home", str(home)))
        if account.pw_shell != login_shell:
            repair_arguments.extend(("--shell", login_shell))
        if len(repair_arguments) > 1:
            self.run(*repair_arguments, name)

    @staticmethod
    def _identity(owner: str, group: str) -> tuple[int, int]:
        try:
            return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid
        except KeyError:
            _fail("filesystem_identity")

    def ensure_directory(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
    ) -> None:
        try:
            if path.is_symlink():
                _fail("managed_directory")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                _fail("managed_directory")
            uid, gid = self._identity(owner, group)
            os.chown(path, uid, gid)
            path.chmod(mode)
        except InstallationError:
            raise
        except OSError:
            _fail("managed_directory")

    def protect_file(
        self,
        path: Path,
        *,
        owner: str,
        group: str,
        mode: int,
        max_bytes: int,
    ) -> bytes:
        try:
            file_status = path.lstat()
            if not stat.S_ISREG(file_status.st_mode) or file_status.st_size > max_bytes:
                _fail("managed_file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                content = stream.read(max_bytes + 1)
                uid, gid = self._identity(owner, group)
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), mode)
            if len(content) > max_bytes:
                _fail("managed_file")
        except InstallationError:
            raise
        except OSError:
            _fail("managed_file")
        else:
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
        temporary_path: Path | None = None
        try:
            if path.is_symlink():
                _fail("managed_file")
            uid, gid = self._identity(owner, group)
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chown(temporary_path, uid, gid)
            temporary_path.chmod(mode)
            temporary_path.replace(path)
            temporary_path = None
        except InstallationError:
            raise
        except OSError:
            _fail("managed_file")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def remove_tree(self, path: Path) -> None:
        if path != _PROBE_ROOT:
            _fail("probe_cleanup")
        try:
            if path.is_symlink():
                _fail("probe_cleanup")
            shutil.rmtree(path, ignore_errors=False)
        except FileNotFoundError:
            return
        except InstallationError:
            raise
        except OSError:
            _fail("probe_cleanup")

    def verify_installation(self) -> None:
        system_verifier().verify()


def main(
    argv: Sequence[str] | None = None,
    *,
    host: InstallationHost | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="specode-review-install",
        description="Install one exact SpeCodeReview release on a supported Ubuntu host.",
    )
    parser.add_argument("--release", required=True)
    arguments = parser.parse_args(argv)
    try:
        NativeHostInstaller(host=host or SystemInstallationHost()).install(arguments.release)
    except WebhookMismatchError as error:
        sys.stderr.write(
            "installation failed: webhook_url_mismatch "
            f"current={error.current} expected={error.expected}\n"
            "Update the GitHub App webhook URL manually, then rerun installation.\n"
        )
        return 1
    except InstallationError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except Exception:  # noqa: BLE001 - keep installer output bounded and secret-free.
        sys.stderr.write("installation failed: unexpected\n")
        return 1
    sys.stdout.write(f"installed SpeCodeReview {arguments.release}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
