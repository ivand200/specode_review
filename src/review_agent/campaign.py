import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_agent.configuration import PROCESS_OUTPUT_MAX_BYTES
from review_agent.fixtures import (
    CampaignFixture,
    CampaignFixtures,
    CreatedFixtureReference,
    FixtureOperations,
    FixturePreparationError,
    prepare_campaign_fixtures,
    subprocess_fixture_operations,
)
from review_agent.models import Sha
from review_agent.process import ProcessOptions, ProcessRunner, _run_bounded_process

_CAMPAIGN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
_MODEL_MAX_CHARS = 128
_SANDBOX_PREFIX_MAX_CHARS = 30
_CAMPAIGN_STAGE_TIMEOUT_SECONDS = 60 * 60
_LIVE_OPT_IN_ENVIRONMENT = frozenset(
    {
        "ACKNOWLEDGE_MODEL_COST",
        "RUN_DOCKER_SANDBOX_E2E",
        "RUN_FULL_LIVE_E2E",
        "RUN_LIVE_GITHUB_E2E",
    }
)


class CampaignStage(StrEnum):
    RUFF = "ruff"
    MYPY = "mypy"
    PYTEST = "pytest"
    DOCKER_SANDBOX = "docker_sandbox"
    FIXTURE_PREPARATION = "fixture_preparation"
    CHECKPOINT_B = "checkpoint_b"
    CHECKPOINT_C = "checkpoint_c"


class CampaignCleanupAction(StrEnum):
    REVIEW = "review_then_close_or_remove"
    DELETE = "delete"
    RETAIN = "retain_as_rollout_evidence"


class CampaignInputError(ValueError):
    """The campaign input cannot safely identify an operator run."""


class CampaignEvidenceError(ValueError):
    """A live profile did not produce its exact bounded success record."""


class CampaignStageOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: CampaignStage
    passed: bool


class CampaignVerifiedResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_run_id: int = Field(gt=0)
    comment_id: int = Field(gt=0)


class CampaignCleanupItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern=r"^(pull_request|branch|comment|check_run)$")
    reference: str = Field(min_length=1, max_length=256)
    action: CampaignCleanupAction


class CampaignSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    succeeded: bool
    repository: str = Field(min_length=1, max_length=201)
    campaign_id: str = Field(min_length=3, max_length=32)
    model: str = Field(min_length=1, max_length=_MODEL_MAX_CHARS)
    evidence_directory: Path
    stages: tuple[CampaignStageOutcome, ...]
    failed_stage: CampaignStage | None = None
    fixtures: CampaignFixtures | None = None
    checkpoint_b: CampaignVerifiedResource | None = None
    checkpoint_c: CampaignVerifiedResource | None = None
    created_resources: tuple[CreatedFixtureReference, ...] = ()
    manual_cleanup: tuple[CampaignCleanupItem, ...] = ()


@dataclass(frozen=True, slots=True)
class CampaignOperations:
    run_stage: Callable[[CampaignStage, Mapping[str, str]], None]
    prepare_fixtures: Callable[
        [str, str, str, Path],
        CampaignFixtures,
    ]


class _ProfileResource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: str
    repository: str
    pr_number: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha
    check_run_id: int = Field(gt=0)
    comment_id: int = Field(gt=0)


def _generated_campaign_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"run-{timestamp}-{secrets.token_hex(3)}"


def _sandbox_prefix(campaign_id: str) -> str:
    direct = f"rae2e-{campaign_id}-"
    if len(direct) <= _SANDBOX_PREFIX_MAX_CHARS:
        return direct
    digest = hashlib.sha256(campaign_id.encode()).hexdigest()[:6]
    return f"rae2e-{campaign_id[:15]}-{digest}-"


def _profile_environment(
    base: Mapping[str, str],
    *,
    repository: str,
    fixture: CampaignFixture,
    resources_path: Path,
) -> dict[str, str]:
    environment = dict(base)
    environment.update(
        {
            "E2E_GITHUB_REPOSITORY": repository,
            "E2E_GITHUB_PR_NUMBER": str(fixture.number),
            "E2E_EXPECTED_BASE_SHA": fixture.base_sha,
            "E2E_EXPECTED_HEAD_SHA": fixture.head_sha,
            "E2E_CREATED_RESOURCES_PATH": str(resources_path),
        }
    )
    return environment


def _verified_profile_resource(
    path: Path,
    *,
    expected_kind: str,
    repository: str,
    fixture: CampaignFixture,
) -> CampaignVerifiedResource:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise CampaignEvidenceError from None
    if len(lines) != 1:
        raise CampaignEvidenceError
    try:
        resource = _ProfileResource.model_validate_json(lines[0])
    except (ValueError, ValidationError):
        raise CampaignEvidenceError from None
    if (
        resource.kind != expected_kind
        or resource.repository.casefold() != repository.casefold()
        or resource.pr_number != fixture.number
        or resource.base_sha.casefold() != fixture.base_sha.casefold()
        or resource.head_sha.casefold() != fixture.head_sha.casefold()
    ):
        raise CampaignEvidenceError
    return CampaignVerifiedResource(
        check_run_id=resource.check_run_id,
        comment_id=resource.comment_id,
    )


def _run_verified_live_profile(  # noqa: PLR0913
    *,
    stage: CampaignStage,
    fixture: CampaignFixture,
    expected_kind: str,
    repository: str,
    base_environment: Mapping[str, str],
    profile_environment: Mapping[str, str],
    evidence_directory: Path,
    operations: CampaignOperations,
) -> CampaignVerifiedResource:
    resources_path = evidence_directory / f"{stage.value.replace('_', '-')}-resources.jsonl"
    environment = _profile_environment(
        {**base_environment, **profile_environment},
        repository=repository,
        fixture=fixture,
        resources_path=resources_path,
    )
    operations.run_stage(stage, environment)
    return _verified_profile_resource(
        resources_path,
        expected_kind=expected_kind,
        repository=repository,
        fixture=fixture,
    )


def _persist_summary(summary: CampaignSummary) -> None:
    path = summary.evidence_directory / "campaign-summary.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(summary.model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _campaign_base_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environment.items()
        if name not in _LIVE_OPT_IN_ENVIRONMENT
    }


def _manual_cleanup(
    *,
    fixtures: CampaignFixtures | None,
    checkpoint_b: CampaignVerifiedResource | None,
    checkpoint_c: CampaignVerifiedResource | None = None,
    created_resources: tuple[CreatedFixtureReference, ...] = (),
) -> tuple[CampaignCleanupItem, ...]:
    items: list[CampaignCleanupItem] = []
    if fixtures is not None:
        items.extend(
            (
                CampaignCleanupItem(
                    kind="pull_request",
                    reference=fixtures.checkpoint_b.url,
                    action=CampaignCleanupAction.REVIEW,
                ),
                CampaignCleanupItem(
                    kind="pull_request",
                    reference=fixtures.checkpoint_c.url,
                    action=CampaignCleanupAction.REVIEW,
                ),
            )
        )
    else:
        items.extend(
            CampaignCleanupItem(
                kind=resource.kind,
                reference=resource.url or resource.branch,
                action=CampaignCleanupAction.REVIEW,
            )
            for resource in created_resources
        )
    for resource in (checkpoint_b, checkpoint_c):
        if resource is None:
            continue
        items.extend(
            (
                CampaignCleanupItem(
                    kind="comment",
                    reference=str(resource.comment_id),
                    action=CampaignCleanupAction.DELETE,
                ),
                CampaignCleanupItem(
                    kind="check_run",
                    reference=str(resource.check_run_id),
                    action=CampaignCleanupAction.RETAIN,
                ),
            )
        )
    return tuple(items)


def _failed_summary(  # noqa: PLR0913
    *,
    repository: str,
    campaign_id: str,
    model: str,
    evidence_directory: Path,
    stages: list[CampaignStageOutcome],
    failed_stage: CampaignStage,
    fixtures: CampaignFixtures | None = None,
    checkpoint_b: CampaignVerifiedResource | None = None,
    created_resources: tuple[CreatedFixtureReference, ...] = (),
) -> CampaignSummary:
    stages.append(CampaignStageOutcome(stage=failed_stage, passed=False))
    summary = CampaignSummary(
        succeeded=False,
        repository=repository,
        campaign_id=campaign_id,
        model=model,
        evidence_directory=evidence_directory,
        stages=tuple(stages),
        failed_stage=failed_stage,
        fixtures=fixtures,
        checkpoint_b=checkpoint_b,
        created_resources=created_resources,
        manual_cleanup=_manual_cleanup(
            fixtures=fixtures,
            checkpoint_b=checkpoint_b,
            created_resources=created_resources,
        ),
    )
    _persist_summary(summary)
    return summary


def run_truthful_real_e2e_campaign(  # noqa: PLR0913
    *,
    repository: str | None,
    model: str | None,
    evidence_root: Path | None,
    campaign_id: str | None,
    environment: Mapping[str, str] = os.environ,
    project_root: Path | None = None,
    operations: CampaignOperations,
) -> CampaignSummary:
    configured_repository = environment.get("GITHUB_REPOSITORY", "")
    resolved_repository = repository or configured_repository
    configured_model = environment.get("CODEX_MODEL", "")
    resolved_model = model or configured_model
    resolved_campaign_id = campaign_id or _generated_campaign_id()
    resolved_project_root = (project_root or Path.cwd()).resolve()
    resolved_evidence_root = (
        evidence_root
        or Path(tempfile.gettempdir()) / "review-agent-real-e2e-campaigns"
    )
    if (
        not resolved_evidence_root.is_absolute()
        or resolved_evidence_root.resolve().is_relative_to(resolved_project_root)
        or _CAMPAIGN_ID.fullmatch(resolved_campaign_id) is None
        or not resolved_model
        or resolved_model.strip() != resolved_model
        or len(resolved_model) > _MODEL_MAX_CHARS
    ):
        raise CampaignInputError

    evidence_directory = resolved_evidence_root / resolved_campaign_id
    evidence_directory.mkdir(parents=True, exist_ok=False)
    fixture_work_root = evidence_directory / "fixture-work"
    state_root = evidence_directory / "state"
    workspace_root = evidence_directory / "workspaces"
    fixture_work_root.mkdir()
    state_root.mkdir(mode=0o700)
    workspace_root.mkdir()

    stages: list[CampaignStageOutcome] = []
    base_environment = _campaign_base_environment(environment)
    for stage in (
        CampaignStage.RUFF,
        CampaignStage.MYPY,
        CampaignStage.PYTEST,
        CampaignStage.DOCKER_SANDBOX,
    ):
        stage_environment = dict(base_environment)
        if stage is CampaignStage.DOCKER_SANDBOX:
            stage_environment.update(
                {
                    "RUN_DOCKER_SANDBOX_E2E": "1",
                    "E2E_SANDBOX_NAME_PREFIX": _sandbox_prefix(resolved_campaign_id),
                }
            )
        try:
            operations.run_stage(stage, stage_environment)
        except Exception:  # noqa: BLE001 - campaign records only the bounded failed stage.
            return _failed_summary(
                repository=resolved_repository,
                campaign_id=resolved_campaign_id,
                model=resolved_model,
                evidence_directory=evidence_directory,
                stages=stages,
                failed_stage=stage,
            )
        stages.append(CampaignStageOutcome(stage=stage, passed=True))

    try:
        fixtures = operations.prepare_fixtures(
            resolved_repository,
            configured_repository,
            resolved_campaign_id,
            fixture_work_root,
        )
    except FixturePreparationError as error:
        return _failed_summary(
            repository=resolved_repository,
            campaign_id=resolved_campaign_id,
            model=resolved_model,
            evidence_directory=evidence_directory,
            stages=stages,
            failed_stage=CampaignStage.FIXTURE_PREPARATION,
            created_resources=error.created_resources,
        )
    except Exception:  # noqa: BLE001 - fixture details are separately bounded by its interface.
        return _failed_summary(
            repository=resolved_repository,
            campaign_id=resolved_campaign_id,
            model=resolved_model,
            evidence_directory=evidence_directory,
            stages=stages,
            failed_stage=CampaignStage.FIXTURE_PREPARATION,
        )
    stages.append(
        CampaignStageOutcome(stage=CampaignStage.FIXTURE_PREPARATION, passed=True)
    )

    profiles = (
        (
            CampaignStage.CHECKPOINT_B,
            fixtures.checkpoint_b,
            "github_check_run_and_pull_request_comment",
            {"RUN_LIVE_GITHUB_E2E": "1"},
        ),
        (
            CampaignStage.CHECKPOINT_C,
            fixtures.checkpoint_c,
            "full_live_github_resources",
            {
                "RUN_FULL_LIVE_E2E": "1",
                "ACKNOWLEDGE_MODEL_COST": "1",
                "E2E_EXPECTED_FINDING": fixtures.expected_finding,
                "E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT": fixtures.instruction_marker,
                "E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT": fixtures.configuration_marker,
                "CODEX_MODEL": resolved_model,
                "STATE_ROOT": str(state_root),
                "WORKSPACE_ROOT": str(workspace_root),
                "SANDBOX_NAME_PREFIX": _sandbox_prefix(resolved_campaign_id),
            },
        ),
    )
    verified_profiles: dict[CampaignStage, CampaignVerifiedResource] = {}
    for stage, fixture, expected_kind, profile_environment in profiles:
        try:
            verified_profiles[stage] = _run_verified_live_profile(
                stage=stage,
                fixture=fixture,
                expected_kind=expected_kind,
                repository=resolved_repository,
                base_environment=base_environment,
                profile_environment=profile_environment,
                evidence_directory=evidence_directory,
                operations=operations,
            )
        except Exception:  # noqa: BLE001 - no unverified profile detail enters the summary.
            return _failed_summary(
                repository=resolved_repository,
                campaign_id=resolved_campaign_id,
                model=resolved_model,
                evidence_directory=evidence_directory,
                stages=stages,
                failed_stage=stage,
                fixtures=fixtures,
                checkpoint_b=verified_profiles.get(CampaignStage.CHECKPOINT_B),
            )
        stages.append(CampaignStageOutcome(stage=stage, passed=True))

    checkpoint_b = verified_profiles[CampaignStage.CHECKPOINT_B]
    checkpoint_c = verified_profiles[CampaignStage.CHECKPOINT_C]

    summary = CampaignSummary(
        succeeded=True,
        repository=resolved_repository,
        campaign_id=resolved_campaign_id,
        model=resolved_model,
        evidence_directory=evidence_directory,
        stages=tuple(stages),
        fixtures=fixtures,
        checkpoint_b=checkpoint_b,
        checkpoint_c=checkpoint_c,
        manual_cleanup=_manual_cleanup(
            fixtures=fixtures,
            checkpoint_b=checkpoint_b,
            checkpoint_c=checkpoint_c,
        ),
    )
    _persist_summary(summary)
    return summary


def subprocess_campaign_operations(
    *,
    runner: ProcessRunner = _run_bounded_process,
) -> CampaignOperations:
    commands = {
        CampaignStage.RUFF: ("uv", "run", "ruff", "check", "."),
        CampaignStage.MYPY: ("uv", "run", "mypy"),
        CampaignStage.PYTEST: ("uv", "run", "pytest", "-q"),
        CampaignStage.DOCKER_SANDBOX: (
            "uv",
            "run",
            "pytest",
            "tests/integration/test_no_model_sandbox_probe.py",
            "-q",
            "-s",
        ),
        CampaignStage.CHECKPOINT_B: (
            "uv",
            "run",
            "pytest",
            "tests/live/test_github_live.py",
            "-q",
            "-s",
        ),
        CampaignStage.CHECKPOINT_C: (
            "uv",
            "run",
            "pytest",
            "tests/live/test_full_live.py",
            "-q",
            "-s",
        ),
    }

    def run_stage(stage: CampaignStage, environment: Mapping[str, str]) -> None:
        arguments = commands.get(stage)
        if arguments is None:
            raise CampaignInputError
        runner(
            arguments,
            ProcessOptions(
                output_max_bytes=PROCESS_OUTPUT_MAX_BYTES,
                stage=f"campaign_{stage.value}",
                timeout_seconds=_CAMPAIGN_STAGE_TIMEOUT_SECONDS,
                use_review_deadline=False,
                env=environment,
            ),
        )

    def prepare(
        repository: str,
        configured_repository: str,
        campaign_id: str,
        work_root: Path,
    ) -> CampaignFixtures:
        fixture_operations: FixtureOperations = subprocess_fixture_operations(
            work_root=work_root,
            runner=runner,
        )
        return prepare_campaign_fixtures(
            repository=repository,
            configured_repository=configured_repository,
            campaign_id=campaign_id,
            operations=fixture_operations,
        )

    return CampaignOperations(
        run_stage=run_stage,
        prepare_fixtures=prepare,
    )


def campaign_main(
    arguments: list[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
    operations: CampaignOperations | None = None,
    output: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(
        prog="review-agent-real-e2e",
        description=(
            "Run the ordered real Review Agent rollout campaign. Invocation authorizes "
            "Docker Sandbox use, GitHub fixture/profile writes, and one model request."
        ),
    )
    parser.add_argument("--repository")
    parser.add_argument("--model")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--campaign-id")
    parsed = parser.parse_args(arguments)
    try:
        summary = run_truthful_real_e2e_campaign(
            repository=parsed.repository,
            model=parsed.model,
            evidence_root=parsed.evidence_root,
            campaign_id=parsed.campaign_id,
            environment=environment,
            operations=operations or subprocess_campaign_operations(),
        )
    except Exception:  # noqa: BLE001 - the CLI exposes only a bounded input-stage failure.
        output.write('{"failed_stage":"input","succeeded":false}\n')
        return 1
    output.write(json.dumps(summary.model_dump(mode="json"), sort_keys=True) + "\n")
    return 0 if summary.succeeded else 1
