import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from time import monotonic as default_monotonic
from time import sleep as default_sleep
from typing import Protocol, TextIO
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from specode_review.accepted_revision import AcceptedRevision
from specode_review.configuration import ProductionServiceSettings
from specode_review.github import GitHubAppClient, ReviewCommentGateway
from specode_review.live import (
    LiveProfileEvidenceError,
    require_fresh_live_review,
    verify_live_review_evidence,
)
from specode_review.models import RepositoryName, ReviewRequest, Sha
from specode_review.publishing import owned_revision_comments

_SERVICE_UNIT = "specode-review.service"
_WORKSPACE_ROOT = Path("/var/lib/specode-review/workspaces")
_ENVIRONMENT_PATH = Path("/opt/specode-review/.env")
_PRIVATE_KEY_PATH = Path("/opt/specode-review/.secrets/github-app.pem")
_OWNED_SANDBOX = re.compile(r"^specode-review-[0-9a-f]{32}$")
_OWNED_WORKSPACE = re.compile(r"^specode-review-workspace-[0-9a-f]{32}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_JOURNAL_BYTES = 1_048_576
_EVIDENCE_TEXT_MAX_CHARS = 256


class CampaignError(RuntimeError):
    """A bounded production-campaign failure safe for operator output."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(f"real E2E campaign failed: {stage}")


class CampaignTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    pr_number: int = Field(gt=0, strict=True)
    base_sha: Sha
    head_sha: Sha
    expected_finding: str = Field(min_length=1, max_length=160)
    expected_path: str = Field(min_length=1, max_length=512)
    expected_line: int = Field(gt=0, strict=True)
    forbidden_repository_text: tuple[str, ...] = Field(default=(), max_length=8)
    forbidden_log_text: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("repository", mode="after")
    @classmethod
    def dedicated_test_repository(cls, value: str) -> str:
        if "test" not in value.partition("/")[2].casefold():
            message = "campaign repository must be a dedicated test repository"
            raise ValueError(message)
        return value.lower()

    @field_validator("expected_path", mode="after")
    @classmethod
    def normalized_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or str(path) != value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            message = "expected path must be normalized and repository-relative"
            raise ValueError(message)
        return value

    @field_validator(
        "expected_finding",
        mode="after",
    )
    @classmethod
    def bounded_expected_finding(cls, value: str) -> str:
        if value.strip() != value or "\n" in value:
            message = "expected finding must be single-line and normalized"
            raise ValueError(message)
        return value

    @field_validator(
        "forbidden_repository_text",
        "forbidden_log_text",
        mode="after",
    )
    @classmethod
    def bounded_forbidden_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item
            or item.strip() != item
            or "\n" in item
            or len(item) > _EVIDENCE_TEXT_MAX_CHARS
            for item in value
        ):
            message = "campaign evidence text must be non-empty, single-line, and bounded"
            raise ValueError(message)
        return value


class CampaignEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    comment_id: int = Field(gt=0, strict=True)
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class CampaignGitHub(ReviewCommentGateway, Protocol):
    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest: ...


class CampaignHost(Protocol):
    def require_installed_service(self) -> None: ...

    def journal_cursor(self) -> str: ...

    def trigger_reopened_event(self, *, repository: str, pr_number: int) -> None: ...

    def journal_lines_after(self, cursor: str) -> tuple[str, ...]: ...

    def owned_resource_names(self) -> tuple[tuple[str, ...], tuple[str, ...]]: ...


def _fail(stage: str) -> None:
    raise CampaignError(stage)


def _expected_revision(target: CampaignTarget) -> AcceptedRevision:
    return AcceptedRevision(
        repository=target.repository,
        pr_number=target.pr_number,
        base_sha=target.base_sha,
        head_sha=target.head_sha,
    )


def _require_no_owned_resources(host: CampaignHost) -> None:
    try:
        sandboxes, workspaces = host.owned_resource_names()
    except Exception:  # noqa: BLE001 - normalize host inspection.
        _fail("resource_inspection")
    if any(_OWNED_SANDBOX.fullmatch(name) for name in sandboxes) or any(
        _OWNED_WORKSPACE.fullmatch(name) for name in workspaces
    ):
        _fail("resource_cleanup")


def _matching_lifecycle_records(
    lines: tuple[str, ...],
    *,
    request: ReviewRequest,
    forbidden_texts: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    revision = AcceptedRevision.from_review_request(request)
    rendered = "\n".join(lines)
    if len(rendered.encode()) > _MAX_JOURNAL_BYTES:
        _fail("log_evidence")
    if any(text.casefold() in rendered.casefold() for text in forbidden_texts):
        _fail("log_redaction")

    records: list[dict[str, object]] = []
    for line in lines:
        if "\n" in line:
            _fail("log_evidence")
        try:
            candidate = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("repository") == revision.repository
            and candidate.get("pull_request") == revision.pr_number
            and candidate.get("accepted_revision") == revision.head_sha
        ):
            records.append(candidate)
    return tuple(records)


def _completed_attempt(records: tuple[dict[str, object], ...]) -> str | None:
    if any(record.get("event") == "normalized_failure" for record in records):
        _fail("technical_failure")
    failed_terminal = any(
        record.get("event") == "terminal_release"
        and record.get("terminal_outcome") != "succeeded"
        for record in records
    )
    if failed_terminal:
        _fail("technical_failure")

    attempts = {
        attempt
        for record in records
        if isinstance((attempt := record.get("attempt_id")), str)
        and _ATTEMPT_ID.fullmatch(attempt)
    }
    accepted_attempts = {
        str(record["attempt_id"])
        for record in records
        if record.get("event") == "admission"
        and record.get("admission_disposition") == "accepted"
        and isinstance(record.get("attempt_id"), str)
        and _ATTEMPT_ID.fullmatch(str(record["attempt_id"]))
    }
    if len(accepted_attempts) > 1:
        _fail("duplicate_execution")
    for attempt_id in attempts:
        attempt_records = [
            record for record in records if record.get("attempt_id") == attempt_id
        ]
        if (
            any(
                record.get("event") == "admission"
                and record.get("admission_disposition") == "accepted"
                for record in attempt_records
            )
            and any(
                record.get("event") == "cleanup"
                and record.get("cleanup_outcome") == "confirmed"
                for record in attempt_records
            )
            and any(
                record.get("event") == "publication"
                and record.get("publication_disposition") in {"created", "reconciled"}
                for record in attempt_records
            )
            and any(
                record.get("event") == "terminal_release"
                and record.get("terminal_outcome") == "succeeded"
                for record in attempt_records
            )
        ):
            return attempt_id
    return None


def run_signed_review_campaign(  # noqa: C901, PLR0913 - one deep campaign transaction.
    *,
    target: CampaignTarget,
    installation_id: int,
    github: CampaignGitHub,
    host: CampaignHost,
    timeout_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float] = default_monotonic,
    sleep: Callable[[float], None] = default_sleep,
) -> CampaignEvidence:
    """Prove one normal signed production review without bypassing webhook ingress."""
    if installation_id < 1 or timeout_seconds <= 0 or poll_seconds <= 0:
        message = "campaign timing and installation identity must be positive"
        raise ValueError(message)

    try:
        host.require_installed_service()
        request = github.review_request(
            pr_number=target.pr_number,
            installation_id=installation_id,
        )
        require_fresh_live_review(
            request=request,
            github=github,
            expected=_expected_revision(target),
        )
    except CampaignError:
        raise
    except Exception:  # noqa: BLE001 - do not expose provider or host output.
        _fail("precondition")

    _require_no_owned_resources(host)
    try:
        cursor = host.journal_cursor()
        host.trigger_reopened_event(
            repository=target.repository,
            pr_number=target.pr_number,
        )
    except Exception:  # noqa: BLE001 - external trigger details are not safe evidence.
        _fail("signed_event")

    deadline = monotonic() + timeout_seconds
    while True:
        try:
            comments = owned_revision_comments(
                request=request,
                gateway=github,
            )
        except Exception:  # noqa: BLE001 - poll transiently until the bounded deadline.
            comments = ()
        if len(comments) > 1:
            _fail("comment_evidence")
        if len(comments) == 1:
            try:
                comment_evidence = verify_live_review_evidence(
                    request=request,
                    github=github,
                    expected_finding=target.expected_finding,
                    expected_path=target.expected_path,
                    expected_line=target.expected_line,
                    forbidden_texts=target.forbidden_repository_text,
                )
            except LiveProfileEvidenceError:
                _fail("comment_evidence")
            try:
                lines = host.journal_lines_after(cursor)
            except Exception:  # noqa: BLE001 - poll host evidence until the deadline.
                lines = ()
            records = _matching_lifecycle_records(
                lines,
                request=request,
                forbidden_texts=(
                    target.expected_finding,
                    *target.forbidden_repository_text,
                    *target.forbidden_log_text,
                ),
            )
            attempt_id = _completed_attempt(records)
            if attempt_id is not None:
                _require_no_owned_resources(host)
                return CampaignEvidence(
                    comment_id=comment_evidence.comment_id,
                    attempt_id=attempt_id,
                )
        if monotonic() >= deadline:
            _fail("timeout")
        sleep(min(poll_seconds, max(0.0, deadline - monotonic())))


class SubprocessCampaignHost:
    """Operate only the installed service, GitHub CLI, journal, and owned resources."""

    def __init__(self, *, public_webhook_url: str) -> None:
        parsed = urlsplit(public_webhook_url)
        if parsed.scheme != "https" or not parsed.netloc:
            message = "public webhook URL must be absolute HTTPS"
            raise ValueError(message)
        self._public_health_url = urlunsplit(
            (parsed.scheme, parsed.netloc, "/health/ready", "", "")
        )

    def _run(self, *arguments: str) -> str:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed commands with validated identities.
                arguments,
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            _fail("host_command")
        if (
            completed.returncode != 0
            or len(completed.stdout) > _MAX_JOURNAL_BYTES
            or len(completed.stderr) > _MAX_JOURNAL_BYTES
        ):
            _fail("host_command")
        return completed.stdout.decode(errors="replace")

    def require_installed_service(self) -> None:
        self._run("systemctl", "is-active", "--quiet", _SERVICE_UNIT)
        self._run(
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "http://127.0.0.1:8000/health/ready",
        )
        self._run(
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            self._public_health_url,
        )

    def journal_cursor(self) -> str:
        output = self._run(
            "journalctl",
            "-u",
            _SERVICE_UNIT,
            "-n",
            "0",
            "--show-cursor",
            "--no-pager",
        )
        marker = "-- cursor: "
        cursors = [
            line.removeprefix(marker)
            for line in output.splitlines()
            if line.startswith(marker)
        ]
        if len(cursors) != 1 or not cursors[0]:
            _fail("log_evidence")
        return cursors[0]

    def trigger_reopened_event(self, *, repository: str, pr_number: int) -> None:
        self._run("gh", "pr", "close", str(pr_number), "--repo", repository)
        self._run("gh", "pr", "reopen", str(pr_number), "--repo", repository)

    def journal_lines_after(self, cursor: str) -> tuple[str, ...]:
        output = self._run(
            "journalctl",
            "-u",
            _SERVICE_UNIT,
            f"--after-cursor={cursor}",
            "-o",
            "cat",
            "--no-pager",
        )
        return tuple(output.splitlines())

    def owned_resource_names(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        sandbox_output = self._run(
            "runuser",
            "-u",
            "specode-review",
            "--",
            "env",
            "-i",
            "HOME=/var/lib/specode-review",
            "PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
            "sbx",
            "ls",
            "--quiet",
        )
        if _WORKSPACE_ROOT.is_symlink() or not _WORKSPACE_ROOT.is_dir():
            _fail("resource_inspection")
        try:
            workspaces = tuple(entry.name for entry in _WORKSPACE_ROOT.iterdir())
        except OSError:
            _fail("resource_inspection")
        return tuple(sandbox_output.splitlines()), workspaces


def _environment_file() -> dict[str, str]:
    try:
        content = _ENVIRONMENT_PATH.read_text(encoding="utf-8")
    except OSError:
        _fail("configuration")
    environment: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or name in environment:
            _fail("configuration")
        environment[name] = value
    return environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specode-review-real-e2e",
        description="Run one signed SpeCodeReview production-path release campaign.",
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--expected-finding", required=True)
    parser.add_argument("--expected-path", required=True)
    parser.add_argument("--expected-line", required=True, type=int)
    parser.add_argument("--forbid-repository-text", action="append", default=[])
    parser.add_argument("--forbid-log-text", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float, default=1_260)
    parser.add_argument("--poll-seconds", type=float, default=5)
    return parser


def campaign_main(
    arguments: Sequence[str] | None = None,
    *,
    output: TextIO = sys.stdout,
) -> int:
    args = _parser().parse_args(arguments)
    github: GitHubAppClient | None = None
    try:
        target = CampaignTarget(
            repository=args.repository,
            pr_number=args.pr_number,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            expected_finding=args.expected_finding,
            expected_path=args.expected_path,
            expected_line=args.expected_line,
            forbidden_repository_text=tuple(args.forbid_repository_text),
            forbidden_log_text=tuple(args.forbid_log_text),
        )
        settings = ProductionServiceSettings.from_environment(_environment_file())
        github = GitHubAppClient(
            repository=target.repository,
            app_id=settings.app_id,
            private_key_path=_PRIVATE_KEY_PATH,
        )
        if github.webhook_url() != settings.public_webhook_url:
            _fail("public_webhook")
        installation_id = github.repository_installation_id()
        evidence = run_signed_review_campaign(
            target=target,
            installation_id=installation_id,
            github=github,
            host=SubprocessCampaignHost(
                public_webhook_url=settings.public_webhook_url,
            ),
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except Exception as error:  # noqa: BLE001 - CLI output is bounded to a safe stage.
        stage = error.stage if isinstance(error, CampaignError) else "input"
        print(json.dumps({"passed": False, "stage": stage}, sort_keys=True), file=output)
        return 1
    finally:
        if github is not None:
            with suppress(Exception):
                github.close()
    print(
        json.dumps(
            {
                "passed": True,
                "comment_id": evidence.comment_id,
                "attempt_id": evidence.attempt_id,
            },
            sort_keys=True,
        ),
        file=output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(campaign_main(sys.argv[1:]))
