import argparse
import os
import pwd
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from time import sleep as default_sleep
from typing import NoReturn, Protocol
from urllib.parse import urlsplit, urlunsplit

from specode_review.configuration import (
    PINNED_CODEX_VERSION,
    PINNED_NGROK_VERSION,
    PINNED_SBX_VERSION,
    ConfigurationError,
    ProductionServiceSettings,
)
from specode_review.github import GitHubAppClient

_APPLICATION_ROOT = Path("/opt/specode-review")
_ENVIRONMENT_PATH = _APPLICATION_ROOT / ".env"
_SERVICE_HOME = Path("/var/lib/specode-review")
_WORKSPACE_ROOT = _SERVICE_HOME / "workspaces"
_REVIEW_KIT = _APPLICATION_ROOT / "review-kit"
_PRIVATE_KEY = _APPLICATION_ROOT / ".secrets/github-app.pem"
_SERVICE_USER = "specode-review"
_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
_PROBE_NAME = "specode-review-verification-probe"
_PROBE_ROOT = _SERVICE_HOME / "verification-probe"
_OWNED_SANDBOX = re.compile(r"^specode-review-[0-9a-f]{32}$")
_OWNED_WORKSPACE = re.compile(r"^specode-review-workspace-[0-9a-f]{32}$")
_SAFE_URL_MAX_CHARS = 2_048
_COMMAND_OUTPUT_MAX_BYTES = 1_048_576
_EVIDENCE = (
    "units",
    "local_health",
    "public_health",
    "github_app",
    "host_tools",
    "review_kit",
    "sandbox_capabilities",
    "resource_cleanup",
)


class VerificationError(RuntimeError):
    """A bounded installation-verification failure safe for operator output."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"verification failed: {stage}")


class WebhookMismatchError(VerificationError):
    def __init__(self, *, current: str, expected: str) -> None:
        if len(current) > _SAFE_URL_MAX_CHARS or len(expected) > _SAFE_URL_MAX_CHARS:
            message = "webhook URLs must be bounded"
            raise ValueError(message)
        self.current = _safe_webhook_url(current)
        self.expected = _safe_webhook_url(expected)
        super().__init__("webhook_url_mismatch")


def _fail(stage: str) -> NoReturn:
    raise VerificationError(stage)


def _safe_webhook_url(value: str) -> str:
    if not value or len(value) > _SAFE_URL_MAX_CHARS:
        return "(invalid)"
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/webhooks/github"
        or parsed.query
        or parsed.fragment
    ):
        return "(invalid)"
    return value


def _public_health_url(webhook_url: str) -> str:
    parsed = urlsplit(webhook_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health/ready", "", ""))


def _parse_environment(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        _fail("configuration")
    environment: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or name.strip() != name or name in environment:
            _fail("configuration")
        environment[name] = value
    return environment


class VerificationHost(Protocol):
    def read_file(self, path: Path, *, max_bytes: int) -> bytes: ...

    def run(self, *arguments: str) -> str: ...

    def owned_workspace_names(self) -> tuple[str, ...]: ...

    def prepare_probe_mounts(self) -> None: ...

    def remove_probe_mounts(self) -> None: ...


class GitHubAppVerification(Protocol):
    def webhook_url(self) -> str: ...

    def close(self) -> None: ...


GitHubFactory = Callable[[int], GitHubAppVerification]


class InstallationVerifier:
    """Repeatably prove the installed host contract without invoking a model."""

    def __init__(
        self,
        *,
        host: VerificationHost,
        github_factory: GitHubFactory,
        sleep: Callable[[float], None] = default_sleep,
        webhook_wait_seconds: float = 600,
        webhook_poll_seconds: float = 5,
    ) -> None:
        if webhook_wait_seconds < 0 or webhook_poll_seconds <= 0:
            message = "webhook wait bounds are invalid"
            raise ValueError(message)
        self._host = host
        self._github_factory = github_factory
        self._sleep = sleep
        self._webhook_wait_seconds = webhook_wait_seconds
        self._webhook_poll_seconds = webhook_poll_seconds

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

    def _run(self, stage: str, *arguments: str) -> str:
        try:
            return self._host.run(*arguments)
        except Exception:  # noqa: BLE001 - normalize all host adapter failures.
            _fail(stage)

    def _settings(self) -> tuple[ProductionServiceSettings, dict[str, str]]:
        try:
            content = self._host.read_file(_ENVIRONMENT_PATH, max_bytes=65_536)
            environment = _parse_environment(content)
            settings = ProductionServiceSettings.from_environment(environment)
        except VerificationError:
            raise
        except ConfigurationError:
            _fail("configuration")
        except Exception:  # noqa: BLE001 - normalize file and parsing failures.
            _fail("configuration")
        return settings, environment

    def _verify_units(self, *, managed_ngrok: bool) -> None:
        units = ["specode-review.service"]
        if managed_ngrok:
            units.append("specode-review-ngrok.service")
        for unit in units:
            self._run("units", "systemctl", "is-enabled", "--quiet", unit)
            self._run("units", "systemctl", "is-active", "--quiet", unit)

    def _verify_health(self, public_webhook_url: str) -> None:
        curl = (
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
        )
        self._run("local_health", *curl, "http://127.0.0.1:8000/health/live")
        self._run("local_health", *curl, "http://127.0.0.1:8000/health/ready")
        self._run("public_health", *curl, _public_health_url(public_webhook_url))

    def _verify_github(self, settings: ProductionServiceSettings) -> None:
        github = self._github_factory(settings.app_id)
        current = "(invalid)"
        try:
            current = github.webhook_url()
            remaining = self._webhook_wait_seconds
            while current != settings.public_webhook_url and remaining > 0:
                wait = min(self._webhook_poll_seconds, remaining)
                self._sleep(wait)
                remaining -= wait
                current = github.webhook_url()
            if current != settings.public_webhook_url:
                self._fail_webhook_mismatch(current, settings.public_webhook_url)
        except WebhookMismatchError:
            raise
        except Exception:  # noqa: BLE001 - normalized provider boundary.
            _fail("github_app")
        finally:
            try:
                github.close()
            except Exception:  # noqa: BLE001 - client closure is part of verification.
                _fail("github_app")

    @staticmethod
    def _fail_webhook_mismatch(current: str, expected: str) -> NoReturn:
        raise WebhookMismatchError(current=current, expected=expected)

    def _verify_tools_and_kit(self, *, managed_ngrok: bool) -> None:
        sbx_version = self._run(
            "host_tools",
            *self._service_command("sbx", "version"),
        ).strip()
        if sbx_version != f"sbx version: v{PINNED_SBX_VERSION}":
            _fail("host_tools")
        codex_version = self._run(
            "host_tools",
            *self._service_command("codex", "--version"),
        ).strip()
        if codex_version != f"codex-cli {PINNED_CODEX_VERSION}":
            _fail("host_tools")
        self._run("host_tools", "git", "--version")
        self._run("host_tools", *self._service_command("sbx", "diagnose"))
        if managed_ngrok:
            ngrok_version = self._run(
                "host_tools",
                *self._service_command("ngrok", "version"),
            ).strip()
            if ngrok_version != f"ngrok version {PINNED_NGROK_VERSION}":
                _fail("host_tools")
        self._run(
            "review_kit",
            *self._service_command("sbx", "kit", "validate", str(_REVIEW_KIT)),
        )

    def _sandbox_names(self, stage: str) -> tuple[str, ...]:
        output = self._run(stage, *self._service_command("sbx", "ls", "--quiet"))
        return tuple(name for raw in output.splitlines() if (name := raw.strip()))

    def _verify_no_stale_resources(self) -> None:
        names = self._sandbox_names("resource_cleanup")
        if any(_OWNED_SANDBOX.fullmatch(name) for name in names):
            _fail("resource_cleanup")
        try:
            workspace_names = self._host.owned_workspace_names()
        except Exception:  # noqa: BLE001 - normalize filesystem inspection.
            _fail("resource_cleanup")
        if any(_OWNED_WORKSPACE.fullmatch(name) for name in workspace_names):
            _fail("resource_cleanup")

    def _probe_sandbox(self) -> None:
        if _PROBE_NAME in self._sandbox_names("sandbox_capabilities"):
            self._run(
                "sandbox_capabilities",
                *self._service_command("sbx", "rm", "--force", _PROBE_NAME),
            )
        created = False
        try:
            self._host.prepare_probe_mounts()
            control = _PROBE_ROOT / "control"
            read_only = _PROBE_ROOT / "readonly"
            self._run(
                "sandbox_capabilities",
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
                ),
            )
            created = True
            self._run(
                "sandbox_capabilities",
                *self._service_command(
                    "sbx",
                    "policy",
                    "deny",
                    "network",
                    "--sandbox",
                    _PROBE_NAME,
                    "**",
                ),
            )
            output = self._run(
                "sandbox_capabilities",
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
                ),
            )
            if output != "probe-ok":
                _fail("sandbox_capabilities")
            self._run(
                "sandbox_capabilities",
                *self._service_command("sbx", "inspect", _PROBE_NAME),
            )
            self._run(
                "sandbox_capabilities",
                *self._service_command("sbx", "policy", "ls", _PROBE_NAME, "--json"),
            )
            self._run(
                "sandbox_capabilities",
                *self._service_command("sbx", "secret", "ls", _PROBE_NAME),
            )
            if _PROBE_NAME not in self._sandbox_names("sandbox_capabilities"):
                _fail("sandbox_capabilities")
        finally:
            if created:
                self._run(
                    "sandbox_capabilities",
                    *self._service_command("sbx", "rm", "--force", _PROBE_NAME),
                )
            try:
                self._host.remove_probe_mounts()
            except Exception:  # noqa: BLE001 - probe cleanup is mandatory.
                _fail("sandbox_capabilities")
        if _PROBE_NAME in self._sandbox_names("resource_cleanup"):
            _fail("resource_cleanup")

    def verify(self) -> tuple[str, ...]:
        settings, environment = self._settings()
        self._verify_units(managed_ngrok=bool(environment.get("NGROK_URL")))
        self._verify_health(settings.public_webhook_url)
        self._verify_github(settings)
        self._verify_tools_and_kit(managed_ngrok=bool(environment.get("NGROK_URL")))
        self._verify_no_stale_resources()
        self._probe_sandbox()
        self._verify_no_stale_resources()
        return _EVIDENCE


class SystemVerificationHost:
    """Root-only adapter for repeatable verification of one installed host."""

    def __init__(self) -> None:
        self._command_timeout_seconds = 300

    def read_file(self, path: Path, *, max_bytes: int) -> bytes:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
                _fail("configuration")
            content = path.read_bytes()
        except VerificationError:
            raise
        except OSError:
            _fail("configuration")
        if len(content) > max_bytes:
            _fail("configuration")
        return content

    def run(self, *arguments: str) -> str:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
            or name.startswith("DOCKER_SANDBOXES_")
        }
        environment["PATH"] = _SAFE_PATH
        try:
            completed = subprocess.run(  # noqa: S603 - verifier-owned bounded commands.
                arguments,
                cwd=_APPLICATION_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                timeout=self._command_timeout_seconds,
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

    def owned_workspace_names(self) -> tuple[str, ...]:
        try:
            if _WORKSPACE_ROOT.is_symlink() or not _WORKSPACE_ROOT.is_dir():
                _fail("resource_cleanup")
            return tuple(entry.name for entry in _WORKSPACE_ROOT.iterdir())
        except VerificationError:
            raise
        except OSError:
            _fail("resource_cleanup")

    @staticmethod
    def _service_identity() -> tuple[int, int]:
        try:
            account = pwd.getpwnam(_SERVICE_USER)
        except KeyError:
            _fail("sandbox_capabilities")
        return account.pw_uid, account.pw_gid

    def prepare_probe_mounts(self) -> None:
        uid, gid = self._service_identity()
        try:
            if _PROBE_ROOT.is_symlink():
                _fail("sandbox_capabilities")
            shutil.rmtree(_PROBE_ROOT, ignore_errors=False)
        except FileNotFoundError:
            pass
        except VerificationError:
            raise
        except OSError:
            _fail("sandbox_capabilities")
        try:
            for path in (_PROBE_ROOT, _PROBE_ROOT / "control", _PROBE_ROOT / "readonly"):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chown(path, uid, gid)
                path.chmod(0o700)
            marker = _PROBE_ROOT / "readonly" / "mount-marker"
            marker.write_bytes(b"probe\n")
            os.chown(marker, uid, gid)
            marker.chmod(0o600)
        except OSError:
            _fail("sandbox_capabilities")

    def remove_probe_mounts(self) -> None:
        try:
            if _PROBE_ROOT.is_symlink():
                _fail("sandbox_capabilities")
            shutil.rmtree(_PROBE_ROOT, ignore_errors=False)
        except FileNotFoundError:
            return
        except VerificationError:
            raise
        except OSError:
            _fail("sandbox_capabilities")


def _github_factory(app_id: int) -> GitHubAppClient:
    return GitHubAppClient(
        repository="specode-review/installation-verification",
        app_id=app_id,
        private_key_path=_PRIVATE_KEY,
    )


def system_verifier() -> InstallationVerifier:
    return InstallationVerifier(
        host=SystemVerificationHost(),
        github_factory=_github_factory,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    verifier: InstallationVerifier | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="specode-review-verify-install",
        description="Verify an installed SpeCodeReview host without invoking a model.",
    )
    parser.parse_args(argv)
    resolved = verifier or system_verifier()
    try:
        evidence = resolved.verify()
    except WebhookMismatchError as error:
        sys.stderr.write(
            "verification failed: webhook_url_mismatch "
            f"current={error.current} expected={error.expected}\n"
            "Update the GitHub App webhook URL manually, then rerun verification.\n"
        )
        return 1
    except VerificationError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except Exception:  # noqa: BLE001 - never expose raw command/provider failures.
        sys.stderr.write("verification failed: unexpected\n")
        return 1
    for item in evidence:
        sys.stdout.write(f"PASS {item}\n")
    sys.stdout.write("verification passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
