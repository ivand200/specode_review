import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import pytest

from review_agent.campaign import (
    CampaignCleanupAction,
    CampaignOperations,
    CampaignStage,
    campaign_main,
    run_truthful_real_e2e_campaign,
    subprocess_campaign_operations,
)
from review_agent.fixtures import (
    CampaignFixture,
    CampaignFixtures,
    CreatedFixtureReference,
    FixturePreparationError,
    FixturePreparationStage,
)
from review_agent.process import ProcessOptions

BASE_SHA = "a" * 40
B_HEAD_SHA = "b" * 40
C_HEAD_SHA = "c" * 40
CAMPAIGN_ID = "release-20260719"
REPOSITORY = "octo-org/test-example"


def _fixtures() -> CampaignFixtures:
    return CampaignFixtures(
        repository=REPOSITORY,
        campaign_id=CAMPAIGN_ID,
        checkpoint_b=CampaignFixture(
            number=101,
            url=f"https://github.com/{REPOSITORY}/pull/101",
            branch=f"review-agent-e2e/{CAMPAIGN_ID}/checkpoint-b",
            base_sha=BASE_SHA,
            head_sha=B_HEAD_SHA,
        ),
        checkpoint_c=CampaignFixture(
            number=102,
            url=f"https://github.com/{REPOSITORY}/pull/102",
            branch=f"review-agent-e2e/{CAMPAIGN_ID}/checkpoint-c",
            base_sha=BASE_SHA,
            head_sha=C_HEAD_SHA,
        ),
        expected_finding="age 18",
        instruction_marker=f"review-agent-e2e-instruction-{CAMPAIGN_ID}",
        configuration_marker=f"review-agent-e2e-config-{CAMPAIGN_ID}",
    )


@dataclass
class ControlledCampaign:
    effects: list[tuple[object, ...]] = field(default_factory=list)
    fail_stage: CampaignStage | None = None

    def run_stage(
        self,
        stage: CampaignStage,
        environment: Mapping[str, str],
    ) -> None:
        captured = dict(environment)
        self.effects.append(("stage", stage, captured))
        if stage is self.fail_stage:
            message = "sensitive subprocess or model output"
            raise RuntimeError(message)
        if stage is CampaignStage.CHECKPOINT_B:
            Path(captured["E2E_CREATED_RESOURCES_PATH"]).write_text(
                json.dumps(
                    {
                        "kind": "github_check_run_and_pull_request_comment",
                        "repository": REPOSITORY,
                        "pr_number": 101,
                        "check_run_id": 501,
                        "comment_id": 601,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        if stage is CampaignStage.CHECKPOINT_C:
            Path(captured["E2E_CREATED_RESOURCES_PATH"]).write_text(
                json.dumps(
                    {
                        "kind": "full_live_github_resources",
                        "repository": REPOSITORY,
                        "pr_number": 102,
                        "check_run_id": 502,
                        "comment_id": 602,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

    def prepare(
        self,
        repository: str,
        configured_repository: str,
        campaign_id: str,
        work_root: Path,
    ) -> CampaignFixtures:
        self.effects.append(
            (
                "fixtures",
                repository,
                configured_repository,
                campaign_id,
                work_root,
            )
        )
        if self.fail_stage is CampaignStage.FIXTURE_PREPARATION:
            message = "sensitive fixture failure"
            raise RuntimeError(message)
        return _fixtures()


def test_operator_campaign_runs_every_gate_in_order_and_reports_verified_evidence(
    tmp_path: Path,
) -> None:
    controlled = ControlledCampaign()
    evidence_root = tmp_path / "campaign-evidence"

    summary = run_truthful_real_e2e_campaign(
        repository=REPOSITORY,
        model=None,
        evidence_root=evidence_root,
        campaign_id=CAMPAIGN_ID,
        environment={
            "GITHUB_REPOSITORY": REPOSITORY,
            "CODEX_MODEL": "configured-model",
        },
        project_root=Path.cwd(),
        operations=CampaignOperations(
            run_stage=controlled.run_stage,
            prepare_fixtures=controlled.prepare,
        ),
    )

    assert summary.succeeded
    assert summary.failed_stage is None
    assert summary.model == "configured-model"
    assert [outcome.stage for outcome in summary.stages] == [
        CampaignStage.RUFF,
        CampaignStage.MYPY,
        CampaignStage.PYTEST,
        CampaignStage.DOCKER_SANDBOX,
        CampaignStage.FIXTURE_PREPARATION,
        CampaignStage.CHECKPOINT_B,
        CampaignStage.CHECKPOINT_C,
    ]
    assert all(outcome.passed for outcome in summary.stages)
    assert summary.fixtures == _fixtures()
    assert summary.checkpoint_b is not None
    assert summary.checkpoint_b.check_run_id == 501
    assert summary.checkpoint_b.comment_id == 601
    assert summary.checkpoint_c is not None
    assert summary.checkpoint_c.check_run_id == 502
    assert summary.checkpoint_c.comment_id == 602
    assert [(item.kind, item.action) for item in summary.manual_cleanup] == [
        ("pull_request", CampaignCleanupAction.REVIEW),
        ("pull_request", CampaignCleanupAction.REVIEW),
        ("comment", CampaignCleanupAction.DELETE),
        ("check_run", CampaignCleanupAction.RETAIN),
        ("comment", CampaignCleanupAction.DELETE),
        ("check_run", CampaignCleanupAction.RETAIN),
    ]

    assert [effect[1] for effect in controlled.effects if effect[0] == "stage"] == [
        CampaignStage.RUFF,
        CampaignStage.MYPY,
        CampaignStage.PYTEST,
        CampaignStage.DOCKER_SANDBOX,
        CampaignStage.CHECKPOINT_B,
        CampaignStage.CHECKPOINT_C,
    ]
    fixture_effect = controlled.effects[4]
    assert fixture_effect[:4] == (
        "fixtures",
        REPOSITORY,
        REPOSITORY,
        CAMPAIGN_ID,
    )
    assert Path(fixture_effect[4]).is_relative_to(evidence_root / CAMPAIGN_ID)

    stage_environments = {
        effect[1]: effect[2]
        for effect in controlled.effects
        if effect[0] == "stage"
    }
    assert stage_environments[CampaignStage.CHECKPOINT_B]["E2E_GITHUB_PR_NUMBER"] == "101"
    checkpoint_c_environment = stage_environments[CampaignStage.CHECKPOINT_C]
    assert checkpoint_c_environment["E2E_GITHUB_PR_NUMBER"] == "102"
    assert checkpoint_c_environment["CODEX_MODEL"] == "configured-model"
    assert checkpoint_c_environment["ACKNOWLEDGE_MODEL_COST"] == "1"
    assert Path(checkpoint_c_environment["STATE_ROOT"]).is_relative_to(
        evidence_root / CAMPAIGN_ID
    )
    assert Path(checkpoint_c_environment["WORKSPACE_ROOT"]).is_relative_to(
        evidence_root / CAMPAIGN_ID
    )
    assert checkpoint_c_environment["SANDBOX_NAME_PREFIX"].startswith(
        f"rae2e-{CAMPAIGN_ID}"
    )
    assert all(
        environment.get("CODEX_MODEL") == "configured-model"
        for stage, environment in stage_environments.items()
        if stage is not CampaignStage.CHECKPOINT_C
    )

    persisted = json.loads(
        (evidence_root / CAMPAIGN_ID / "campaign-summary.json").read_text()
    )
    assert persisted["succeeded"] is True
    assert persisted["checkpoint_c"]["comment_id"] == 602


@pytest.mark.parametrize("failed_stage", list(CampaignStage))
def test_campaign_stops_at_first_failure_without_promoting_unverified_evidence(
    tmp_path: Path,
    failed_stage: CampaignStage,
) -> None:
    controlled = ControlledCampaign(fail_stage=failed_stage)

    summary = run_truthful_real_e2e_campaign(
        repository=REPOSITORY,
        model=None,
        evidence_root=tmp_path / failed_stage.value,
        campaign_id=CAMPAIGN_ID,
        environment={
            "GITHUB_REPOSITORY": REPOSITORY,
            "CODEX_MODEL": "configured-model",
        },
        project_root=Path.cwd(),
        operations=CampaignOperations(
            run_stage=controlled.run_stage,
            prepare_fixtures=controlled.prepare,
        ),
    )

    assert not summary.succeeded
    assert summary.failed_stage is failed_stage
    assert summary.stages[-1].stage is failed_stage
    assert not summary.stages[-1].passed
    assert [outcome.stage for outcome in summary.stages].index(failed_stage) == (
        len(summary.stages) - 1
    )
    failed_index = list(CampaignStage).index(failed_stage)
    fixture_index = list(CampaignStage).index(CampaignStage.FIXTURE_PREPARATION)
    assert (summary.fixtures is not None) == (failed_index > fixture_index)
    assert (summary.checkpoint_b is not None) == (
        failed_stage is CampaignStage.CHECKPOINT_C
    )
    assert summary.checkpoint_c is None

    observed = [
        effect[1] if effect[0] == "stage" else CampaignStage.FIXTURE_PREPARATION
        for effect in controlled.effects
    ]
    expected = list(CampaignStage)
    assert observed == expected[: expected.index(failed_stage) + 1]
    persisted = (
        tmp_path / failed_stage.value / CAMPAIGN_ID / "campaign-summary.json"
    ).read_text()
    assert "sensitive" not in persisted
    assert "subprocess" not in persisted
    assert "model output" not in persisted


def test_explicit_model_override_is_scoped_to_checkpoint_c(tmp_path: Path) -> None:
    controlled = ControlledCampaign()

    summary = run_truthful_real_e2e_campaign(
        repository=None,
        model="approved-override",
        evidence_root=tmp_path / "override",
        campaign_id=CAMPAIGN_ID,
        environment={
            "GITHUB_REPOSITORY": REPOSITORY,
            "CODEX_MODEL": "configured-model",
            "RUN_FULL_LIVE_E2E": "1",
            "ACKNOWLEDGE_MODEL_COST": "1",
        },
        project_root=Path.cwd(),
        operations=CampaignOperations(
            run_stage=controlled.run_stage,
            prepare_fixtures=controlled.prepare,
        ),
    )

    assert summary.succeeded
    stage_environments = {
        effect[1]: effect[2]
        for effect in controlled.effects
        if effect[0] == "stage"
    }
    assert stage_environments[CampaignStage.RUFF]["CODEX_MODEL"] == "configured-model"
    assert "RUN_FULL_LIVE_E2E" not in stage_environments[CampaignStage.RUFF]
    assert "ACKNOWLEDGE_MODEL_COST" not in stage_environments[CampaignStage.RUFF]
    assert (
        stage_environments[CampaignStage.CHECKPOINT_C]["CODEX_MODEL"]
        == "approved-override"
    )


@dataclass
class ControlledProcessRunner:
    calls: list[tuple[tuple[str, ...], ProcessOptions]] = field(default_factory=list)

    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, stdout=b"", stderr=b"")


def test_subprocess_campaign_boundary_reuses_the_existing_profile_commands() -> None:
    runner = ControlledProcessRunner()
    operations = subprocess_campaign_operations(runner=runner)
    environment = {"PATH": "/usr/bin", "CAMPAIGN_SENTINEL": "present"}

    for stage in (
        CampaignStage.RUFF,
        CampaignStage.MYPY,
        CampaignStage.PYTEST,
        CampaignStage.DOCKER_SANDBOX,
        CampaignStage.CHECKPOINT_B,
        CampaignStage.CHECKPOINT_C,
    ):
        operations.run_stage(stage, environment)

    assert [call[0] for call in runner.calls] == [
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "mypy"),
        ("uv", "run", "pytest", "-q"),
        (
            "uv",
            "run",
            "pytest",
            "tests/integration/test_no_model_sandbox_probe.py",
            "-q",
            "-s",
        ),
        (
            "uv",
            "run",
            "pytest",
            "tests/live/test_github_live.py",
            "-q",
            "-s",
        ),
        (
            "uv",
            "run",
            "pytest",
            "tests/live/test_full_live.py",
            "-q",
            "-s",
        ),
    ]
    assert all(call[1].env == environment for call in runner.calls)
    assert all(call[1].output_max_bytes == 1_048_576 for call in runner.calls)
    assert all(not call[1].use_review_deadline for call in runner.calls)


def test_campaign_command_returns_nonzero_and_only_bounded_failure_json(
    tmp_path: Path,
) -> None:
    controlled = ControlledCampaign(fail_stage=CampaignStage.CHECKPOINT_B)
    output = StringIO()

    exit_status = campaign_main(
        [
            "--repository",
            REPOSITORY,
            "--campaign-id",
            CAMPAIGN_ID,
            "--evidence-root",
            str(tmp_path / "cli"),
        ],
        environment={
            "GITHUB_REPOSITORY": REPOSITORY,
            "CODEX_MODEL": "configured-model",
        },
        operations=CampaignOperations(
            run_stage=controlled.run_stage,
            prepare_fixtures=controlled.prepare,
        ),
        output=output,
    )

    rendered = output.getvalue()
    assert exit_status == 1
    assert json.loads(rendered)["failed_stage"] == "checkpoint_b"
    assert "sensitive" not in rendered
    assert "model output" not in rendered


def test_partial_fixture_failure_reports_only_bounded_created_references(
    tmp_path: Path,
) -> None:
    controlled = ControlledCampaign()
    created = CreatedFixtureReference(
        kind="pull_request",
        branch=f"review-agent-e2e/{CAMPAIGN_ID}/checkpoint-b",
        number=101,
        url=f"https://github.com/{REPOSITORY}/pull/101",
    )

    def fail_preparation(
        repository: str,
        configured_repository: str,
        campaign_id: str,
        work_root: Path,
    ) -> CampaignFixtures:
        del repository, configured_repository, campaign_id, work_root
        raise FixturePreparationError(
            FixturePreparationStage.CHECKPOINT_C_PUSH,
            created_resources=(created,),
        )

    summary = run_truthful_real_e2e_campaign(
        repository=REPOSITORY,
        model=None,
        evidence_root=tmp_path / "partial",
        campaign_id=CAMPAIGN_ID,
        environment={
            "GITHUB_REPOSITORY": REPOSITORY,
            "CODEX_MODEL": "configured-model",
        },
        project_root=Path.cwd(),
        operations=CampaignOperations(
            run_stage=controlled.run_stage,
            prepare_fixtures=fail_preparation,
        ),
    )

    assert not summary.succeeded
    assert summary.failed_stage is CampaignStage.FIXTURE_PREPARATION
    assert summary.created_resources == (created,)
    assert summary.manual_cleanup[0].reference == created.url
    assert summary.manual_cleanup[0].action is CampaignCleanupAction.REVIEW
    persisted = (
        tmp_path / "partial" / CAMPAIGN_ID / "campaign-summary.json"
    ).read_text()
    assert "checkpoint_c_push" not in persisted
