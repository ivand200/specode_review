import json
from pathlib import Path

import pytest

from review_agent.github import (
    CheckRun,
    CheckRunConclusion,
    CheckRunStatus,
    ReviewComment,
    ReviewCommentApp,
    ReviewIdentity,
    derive_review_identity,
)
from review_agent.live import LiveProfileEvidenceError, verify_live_review_evidence
from review_agent.models import ReviewRequest
from tests.live.test_full_live import _finish_checkpoint_c


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Truthful live evidence",
        description="",
    )


def _check_run(identity: ReviewIdentity) -> CheckRun:
    return CheckRun.model_validate(
        {
            "id": 91,
            "name": "Review Agent",
            "head_sha": identity.head_sha,
            "external_id": identity.external_id,
            "status": CheckRunStatus.COMPLETED,
            "conclusion": CheckRunConclusion.NEUTRAL,
            "app": {"id": 12345},
            "output": {
                "title": "Review complete — findings published",
                "summary": "Review completed with 1 important finding.",
            },
        }
    )


def _comment(identity: ReviewIdentity) -> ReviewComment:
    return ReviewComment(
        id=72,
        body=(
            "# Automated code review\n\n"
            "The seeded defect loses saved data.\n\n"
            f"<!-- {identity.external_id} -->\n"
        ),
        performed_via_github_app=ReviewCommentApp(id=12345),
    )


class EvidenceGateway:
    def __init__(
        self,
        *,
        check_runs: tuple[CheckRun, ...],
        comments: tuple[ReviewComment, ...],
    ) -> None:
        self.check_runs = check_runs
        self.comments = comments

    @property
    def app_id(self) -> int:
        return 12345

    def list_check_runs(
        self,
        *,
        identity: ReviewIdentity,
        installation_id: int,
    ) -> tuple[CheckRun, ...]:
        assert identity == derive_review_identity(_request())
        assert installation_id == 23
        return self.check_runs

    def is_owned_check_run(
        self,
        check_run: CheckRun,
        *,
        identity: ReviewIdentity,
    ) -> bool:
        return (
            check_run.app.id == self.app_id
            and check_run.name == "Review Agent"
            and check_run.head_sha == identity.head_sha
            and check_run.external_id == identity.external_id
        )

    def list_review_comments(
        self,
        *,
        repository: str,
        pr_number: int,
        installation_id: int,
    ) -> tuple[ReviewComment, ...]:
        assert (repository, pr_number, installation_id) == (
            "Octo-Org/Example",
            17,
            23,
        )
        return self.comments


def test_checkpoint_c_confirms_exact_findings_evidence_through_typed_gateways() -> None:
    request = _request()
    identity = derive_review_identity(request)
    gateway = EvidenceGateway(
        check_runs=(_check_run(identity),),
        comments=(_comment(identity),),
    )

    evidence = verify_live_review_evidence(
        request=request,
        github=gateway,
        expected_finding="seeded defect",
        forbidden_texts=("hostile instruction", "hostile config"),
    )

    assert evidence.check_run_id == 91
    assert evidence.comment_id == 72


def test_checkpoint_c_rejects_a_neutral_non_findings_presentation() -> None:
    request = _request()
    identity = derive_review_identity(request)
    check_run = _check_run(identity)
    gateway = EvidenceGateway(
        check_runs=(
            check_run.model_copy(
                update={
                    "output": check_run.output.model_copy(
                        update={"title": "Review incomplete — technical failure"}
                    )
                }
            ),
        ),
        comments=(_comment(identity),),
    )

    with pytest.raises(LiveProfileEvidenceError, match="findings Check Run"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction", "hostile config"),
        )


@pytest.mark.parametrize(
    ("status", "conclusion", "title"),
    [
        (
            CheckRunStatus.COMPLETED,
            CheckRunConclusion.SUCCESS,
            "Review complete — no important findings",
        ),
        (
            CheckRunStatus.COMPLETED,
            CheckRunConclusion.NEUTRAL,
            "Review incomplete — timeout",
        ),
        (
            CheckRunStatus.COMPLETED,
            CheckRunConclusion.NEUTRAL,
            "Review incomplete — publication state unknown",
        ),
        (
            CheckRunStatus.IN_PROGRESS,
            None,
            "Review in progress",
        ),
    ],
)
def test_checkpoint_c_rejects_every_non_findings_terminal_shape(
    status: CheckRunStatus,
    conclusion: CheckRunConclusion | None,
    title: str,
) -> None:
    request = _request()
    identity = derive_review_identity(request)
    check_run = _check_run(identity)
    gateway = EvidenceGateway(
        check_runs=(
            check_run.model_copy(
                update={
                    "status": status,
                    "conclusion": conclusion,
                    "output": check_run.output.model_copy(update={"title": title}),
                }
            ),
        ),
        comments=(_comment(identity),),
    )

    with pytest.raises(LiveProfileEvidenceError, match="findings Check Run"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction", "hostile config"),
        )


@pytest.mark.parametrize(
    "comments",
    [
        (),
        (
            ReviewComment(
                id=72,
                body="foreign app\n\n"
                f"<!-- {derive_review_identity(_request()).external_id} -->\n",
                performed_via_github_app=ReviewCommentApp(id=54321),
            ),
        ),
        (
            ReviewComment(
                id=72,
                body=f"wrong marker\n\n<!-- review-agent:v1:{'c' * 64} -->\n",
                performed_via_github_app=ReviewCommentApp(id=12345),
            ),
        ),
        (
            _comment(derive_review_identity(_request())),
            _comment(derive_review_identity(_request())).model_copy(update={"id": 73}),
        ),
    ],
)
def test_checkpoint_c_rejects_missing_foreign_wrong_marker_and_duplicate_comments(
    comments: tuple[ReviewComment, ...],
) -> None:
    request = _request()
    identity = derive_review_identity(request)
    gateway = EvidenceGateway(check_runs=(_check_run(identity),), comments=comments)

    with pytest.raises(LiveProfileEvidenceError, match="application-owned comment"):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction", "hostile config"),
        )


@pytest.mark.parametrize(
    ("comment_body", "message"),
    [
        (
            "# Automated code review\n\nA different issue.\n",
            "expected finding",
        ),
        (
            "# Automated code review\n\nseeded defect\nhostile instruction\n",
            "forbidden repository text",
        ),
        (
            "# Automated code review\n\nseeded defect\nhostile config\n",
            "forbidden repository text",
        ),
    ],
)
def test_checkpoint_c_rejects_unproven_or_hostile_comment_content(
    comment_body: str,
    message: str,
) -> None:
    request = _request()
    identity = derive_review_identity(request)
    comment = _comment(identity).model_copy(
        update={"body": f"{comment_body}\n<!-- {identity.external_id} -->\n"}
    )
    gateway = EvidenceGateway(
        check_runs=(_check_run(identity),),
        comments=(comment,),
    )

    with pytest.raises(LiveProfileEvidenceError, match=message):
        verify_live_review_evidence(
            request=request,
            github=gateway,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction", "hostile config"),
        )


class SandboxInventory:
    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self.names = names

    def list_names(self) -> tuple[str, ...]:
        return self.names


def test_checkpoint_c_records_only_the_ids_confirmed_after_cleanup(tmp_path: Path) -> None:
    request = _request()
    identity = derive_review_identity(request)
    gateway = EvidenceGateway(
        check_runs=(_check_run(identity),),
        comments=(_comment(identity),),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    resources_path = tmp_path / "resources.jsonl"

    _finish_checkpoint_c(
        github=gateway,  # type: ignore[arg-type]
        request=request,
        running_check_run_id=91,
        expected_finding="seeded defect",
        forbidden_texts=("hostile instruction", "hostile config"),
        workspace_root=workspace_root,
        sandbox_client=SandboxInventory(),  # type: ignore[arg-type]
        sandbox_name_prefix="review-agent-",
        resources_path=resources_path,
    )

    record = json.loads(resources_path.read_text())
    assert record == {
        "kind": "full_live_github_resources",
        "repository": "Octo-Org/Example",
        "pr_number": 17,
        "check_run_id": 91,
        "comment_id": 72,
        "cleanup": (
            "delete the recorded pull-request comment; retain the Check Run as rollout evidence"
        ),
    }
    assert "seeded defect" not in resources_path.read_text()


@pytest.mark.parametrize(
    "failed_gate",
    ["evidence", "running_identity", "workspace_cleanup", "sandbox_cleanup"],
)
def test_checkpoint_c_appends_no_resource_record_when_a_final_gate_fails(
    failed_gate: str,
    tmp_path: Path,
) -> None:
    request = _request()
    identity = derive_review_identity(request)
    check_run = _check_run(identity)
    if failed_gate == "evidence":
        check_run = check_run.model_copy(
            update={
                "output": check_run.output.model_copy(
                    update={"title": "Review incomplete — technical failure"}
                )
            }
        )
    gateway = EvidenceGateway(
        check_runs=(check_run,),
        comments=(_comment(identity),),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    if failed_gate == "workspace_cleanup":
        (workspace_root / "review-agent-workspace-stale").mkdir()
    sandbox_names = (
        ("review-agent-stale",) if failed_gate == "sandbox_cleanup" else ()
    )
    resources_path = tmp_path / "resources.jsonl"

    with pytest.raises((AssertionError, LiveProfileEvidenceError)):
        _finish_checkpoint_c(
            github=gateway,  # type: ignore[arg-type]
            request=request,
            running_check_run_id=92 if failed_gate == "running_identity" else 91,
            expected_finding="seeded defect",
            forbidden_texts=("hostile instruction", "hostile config"),
            workspace_root=workspace_root,
            sandbox_client=SandboxInventory(sandbox_names),  # type: ignore[arg-type]
            sandbox_name_prefix="review-agent-",
            resources_path=resources_path,
        )

    assert not resources_path.exists()
