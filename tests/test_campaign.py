import json
from dataclasses import dataclass, field

import pytest

from specode_review.accepted_revision import AcceptedRevision
from specode_review.campaign import (
    CampaignError,
    CampaignEvidence,
    CampaignTarget,
    run_signed_review_campaign,
)
from specode_review.github import (
    ReviewComment,
    ReviewCommentApp,
)
from specode_review.models import ReviewRequest


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/specode-review-test",
        pr_number=42,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Seed a stable changed-line defect",
        description="",
    )


@dataclass
class ControlledGitHub:
    request: ReviewRequest
    comments: tuple[ReviewComment, ...] = ()

    @property
    def app_id(self) -> int:
        return 12345

    def review_request(self, *, pr_number: int, installation_id: int) -> ReviewRequest:
        assert (pr_number, installation_id) == (
            self.request.pr_number,
            self.request.installation_id,
        )
        return self.request

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        assert (repository, pr_number, installation_id) == (
            self.request.repository,
            self.request.pr_number,
            self.request.installation_id,
        )
        return self.comments


@dataclass
class ControlledHost:
    github: ControlledGitHub
    request: ReviewRequest
    effects: list[str] = field(default_factory=list)

    def require_installed_service(self) -> None:
        self.effects.append("installed_service")

    def journal_cursor(self) -> str:
        self.effects.append("journal_cursor")
        return "cursor-before-trigger"

    def trigger_reopened_event(self, *, repository: str, pr_number: int) -> None:
        assert (repository, pr_number) == (
            self.request.repository,
            self.request.pr_number,
        )
        self.effects.append("github_reopened")
        marker = (
            f"<!-- {AcceptedRevision.from_review_request(self.request).external_id} -->"
        )
        self.github.comments = (
            ReviewComment(
                id=91,
                body=(
                    "# Automated code review\n\n"
                    "### Finding 1: ` Age 18 is incorrectly rejected `\n\n"
                    "- Severity: ` important `\n"
                    "- Locations:\n"
                    "  - ` campaign-fixtures/release/adult_age.py:4 `\n"
                    "- Evidence: ` The strict comparison rejects age 18. `\n\n"
                    f"{marker}\n"
                ),
                performed_via_github_app=ReviewCommentApp(id=self.github.app_id),
            ),
        )

    def journal_lines_after(self, cursor: str) -> tuple[str, ...]:
        assert cursor == "cursor-before-trigger"
        self.effects.append("journal")
        common = {
            "repository": self.request.repository,
            "pull_request": self.request.pr_number,
            "accepted_revision": self.request.head_sha,
            "attempt_id": "e" * 32,
        }
        return tuple(
            json.dumps({**common, **record}, separators=(",", ":"))
            for record in (
                {"event": "admission", "admission_disposition": "accepted"},
                {"event": "cleanup", "cleanup_outcome": "confirmed"},
                {"event": "publication", "publication_disposition": "created"},
                {"event": "terminal_release", "terminal_outcome": "succeeded"},
            )
        )

    def owned_resource_names(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        self.effects.append("resources")
        return (), ()


def test_campaign_accepts_one_grounded_signed_review_with_safe_logs_and_cleanup() -> None:
    request = _request()
    github = ControlledGitHub(request)
    host = ControlledHost(github=github, request=request)

    evidence = run_signed_review_campaign(
        target=CampaignTarget(
            repository=request.repository,
            pr_number=request.pr_number,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
            expected_finding="age 18",
            expected_path="campaign-fixtures/release/adult_age.py",
            expected_line=4,
            forbidden_repository_text=(
                "specode-review-e2e-instruction-release",
                "specode-review-e2e-config-release",
            ),
            forbidden_log_text=(
                "return age > 18",
                "candidate-sentinel",
                "prompt-sentinel",
            ),
        ),
        installation_id=request.installation_id,
        github=github,
        host=host,
        timeout_seconds=10,
        poll_seconds=1,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert evidence == CampaignEvidence(comment_id=91, attempt_id="e" * 32)
    assert host.effects == [
        "installed_service",
        "resources",
        "journal_cursor",
        "github_reopened",
        "journal",
        "resources",
    ]


def test_campaign_rejects_duplicate_review_execution_evidence() -> None:
    request = _request()
    github = ControlledGitHub(request)

    class DuplicateAttemptHost(ControlledHost):
        def journal_lines_after(self, cursor: str) -> tuple[str, ...]:
            original = super().journal_lines_after(cursor)
            duplicate = tuple(
                line.replace("e" * 32, "f" * 32)
                for line in original
            )
            return (*original, *duplicate)

    host = DuplicateAttemptHost(github=github, request=request)

    with pytest.raises(CampaignError, match="duplicate_execution"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding="age 18",
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
            ),
            installation_id=request.installation_id,
            github=github,
            host=host,
            timeout_seconds=10,
            poll_seconds=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


@pytest.mark.parametrize(
    ("replacement", "expected_finding"),
    [
        (("incorrectly rejected", "not discussed"), "incorrectly rejected"),
        (("campaign-fixtures/release/adult_age.py:4", "other.py:9"), "age 18"),
        (("important", "minor"), "age 18"),
    ],
)
def test_campaign_rejects_semantically_invalid_comment_evidence(
    replacement: tuple[str, str],
    expected_finding: str,
) -> None:
    request = _request()
    github = ControlledGitHub(request)

    class InvalidCommentHost(ControlledHost):
        def trigger_reopened_event(self, *, repository: str, pr_number: int) -> None:
            super().trigger_reopened_event(repository=repository, pr_number=pr_number)
            comment = self.github.comments[0]
            self.github.comments = (
                comment.model_copy(
                    update={"body": comment.body.replace(*replacement)},
                ),
            )

    with pytest.raises(CampaignError, match="comment_evidence"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding=expected_finding,
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
            ),
            installation_id=request.installation_id,
            github=github,
            host=InvalidCommentHost(github=github, request=request),
            timeout_seconds=10,
            poll_seconds=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_campaign_rejects_sensitive_log_content() -> None:
    request = _request()
    github = ControlledGitHub(request)

    class UnsafeHost(ControlledHost):
        def journal_lines_after(self, cursor: str) -> tuple[str, ...]:
            return (*super().journal_lines_after(cursor), "prompt-sentinel")

    with pytest.raises(CampaignError, match="log_redaction"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding="age 18",
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
                forbidden_log_text=("prompt-sentinel",),
            ),
            installation_id=request.installation_id,
            github=github,
            host=UnsafeHost(github=github, request=request),
            timeout_seconds=10,
            poll_seconds=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_campaign_rejects_a_completed_review_with_owned_resources_left_behind() -> None:
    request = _request()
    github = ControlledGitHub(request)

    class UncleanHost(ControlledHost):
        resource_reads = 0

        def owned_resource_names(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
            self.resource_reads += 1
            if self.resource_reads == 1:
                return super().owned_resource_names()
            return (), ("specode-review-workspace-" + "f" * 32,)

    with pytest.raises(CampaignError, match="resource_cleanup"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding="age 18",
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
            ),
            installation_id=request.installation_id,
            github=github,
            host=UncleanHost(github=github, request=request),
            timeout_seconds=10,
            poll_seconds=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_campaign_rejects_normalized_technical_failure_evidence() -> None:
    request = _request()
    github = ControlledGitHub(request)

    class FailedHost(ControlledHost):
        def journal_lines_after(self, cursor: str) -> tuple[str, ...]:
            records = super().journal_lines_after(cursor)
            failed = json.loads(records[-1])
            failed.update(
                {
                    "event": "normalized_failure",
                    "stage": "review",
                    "category": "review_failure",
                }
            )
            return (*records, json.dumps(failed))

    with pytest.raises(CampaignError, match="technical_failure"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding="age 18",
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
            ),
            installation_id=request.installation_id,
            github=github,
            host=FailedHost(github=github, request=request),
            timeout_seconds=10,
            poll_seconds=1,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_campaign_times_out_when_the_signed_event_publishes_no_comment() -> None:
    request = _request()
    github = ControlledGitHub(request)
    ticks = iter((0.0, 0.0, 1.0, 1.0))

    class MissingCommentHost(ControlledHost):
        def trigger_reopened_event(self, *, repository: str, pr_number: int) -> None:
            assert (repository, pr_number) == (
                self.request.repository,
                self.request.pr_number,
            )
            self.effects.append("github_reopened")

    with pytest.raises(CampaignError, match="timeout"):
        run_signed_review_campaign(
            target=CampaignTarget(
                repository=request.repository,
                pr_number=request.pr_number,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                expected_finding="age 18",
                expected_path="campaign-fixtures/release/adult_age.py",
                expected_line=4,
            ),
            installation_id=request.installation_id,
            github=github,
            host=MissingCommentHost(github=github, request=request),
            timeout_seconds=1,
            poll_seconds=1,
            monotonic=lambda: next(ticks),
            sleep=lambda _: None,
        )
