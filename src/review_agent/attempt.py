import logging
import os
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Annotated, BinaryIO, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from review_agent.configuration import AttemptSettings
from review_agent.core import CandidateAcceptance, GitHubRepository, Reviewer
from review_agent.deadline import ReviewDeadline, review_deadline_scope
from review_agent.errors import FailureCategory, ReviewError
from review_agent.github import GitHubAppClient
from review_agent.models import ReviewRequest, ReviewResult
from review_agent.publishing import publish_review_result
from review_agent.resources import ReviewResourceManager
from review_agent.sandbox import CodexSandboxAdapter, DockerSandboxClient

ATTEMPT_COMMAND_MAX_BYTES = 65_536
ATTEMPT_OUTCOME_MAX_BYTES = 4_096

AttemptId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
]
FailureStage = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$"),
]


class AttemptCommandError(ValueError):
    """A normalized, payload-safe launch-contract failure."""

    def __init__(self) -> None:
        super().__init__("invalid attempt command")


class AttemptCommand(BaseModel):
    """The complete immutable command delivered to one review child."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: AttemptId
    check_run_id: int | None = Field(default=None, gt=0, strict=True)
    outcome_fd: int | None = Field(default=None, ge=3, strict=True)
    request: ReviewRequest

    @model_validator(mode="after")
    def check_run_requires_outcome_channel(self) -> "AttemptCommand":
        if (self.check_run_id is None) != (self.outcome_fd is None):
            message = "check run and outcome channel must be provided together"
            raise ValueError(message)
        return self

    def to_json_bytes(self) -> bytes:
        document = self.model_dump_json().encode()
        if len(document) > ATTEMPT_COMMAND_MAX_BYTES:
            raise AttemptCommandError
        return document

    @classmethod
    def from_json_bytes(cls, document: bytes) -> Self:
        if len(document) > ATTEMPT_COMMAND_MAX_BYTES:
            raise AttemptCommandError
        try:
            return cls.model_validate_json(document, strict=True)
        except (TypeError, ValueError):
            raise AttemptCommandError from None


class AttemptOutcomeError(ValueError):
    """A normalized, payload-safe child-outcome failure."""

    def __init__(self) -> None:
        super().__init__("invalid attempt outcome")


class AttemptStatus(StrEnum):
    REVIEWED = "reviewed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class AttemptPublication(StrEnum):
    PUBLISHED = "published"
    NOT_ATTEMPTED = "not_attempted"
    UNKNOWN = "unknown"


class AttemptOutcome(BaseModel):
    """The complete bounded result returned by one review child."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: AttemptId
    status: AttemptStatus
    review_status: Literal["no_important_issues", "issues_found"] | None
    publication: AttemptPublication
    failure_stage: FailureStage | None
    failure_category: FailureCategory | None

    @model_validator(mode="after")
    def fields_describe_one_trustworthy_state(self) -> "AttemptOutcome":
        if (self.failure_stage is None) != (self.failure_category is None):
            message = "failure stage and category must be provided together"
            raise ValueError(message)
        if self.status is AttemptStatus.REVIEWED:
            _validate_reviewed_outcome(self)
        else:
            _validate_incomplete_outcome(self)
        return self

    def to_json_bytes(self) -> bytes:
        document = self.model_dump_json().encode()
        if len(document) > ATTEMPT_OUTCOME_MAX_BYTES:
            raise AttemptOutcomeError
        return document

    @classmethod
    def from_json_bytes(
        cls,
        document: bytes,
        *,
        expected_attempt_id: str,
    ) -> Self:
        if len(document) > ATTEMPT_OUTCOME_MAX_BYTES:
            raise AttemptOutcomeError
        try:
            outcome = cls.model_validate_json(document, strict=True)
        except (TypeError, ValueError):
            raise AttemptOutcomeError from None
        if outcome.attempt_id != expected_attempt_id:
            raise AttemptOutcomeError
        return outcome


def _validate_reviewed_outcome(outcome: AttemptOutcome) -> None:
    if (
        outcome.review_status is None
        or outcome.publication is not AttemptPublication.PUBLISHED
        or outcome.failure_stage is not None
    ):
        message = "reviewed outcomes require one trusted published result"
        raise ValueError(message)


def _validate_incomplete_outcome(outcome: AttemptOutcome) -> None:
    if outcome.failure_stage is None:
        message = "incomplete outcomes require normalized failure details"
        raise ValueError(message)
    if (
        outcome.status is AttemptStatus.TIMED_OUT
        and outcome.failure_category is not FailureCategory.TIMEOUT
    ):
        message = "timed out outcomes require the timeout category"
        raise ValueError(message)
    if (
        outcome.status is AttemptStatus.FAILED
        and outcome.failure_category is FailureCategory.TIMEOUT
    ):
        message = "timeout failures require timed_out status"
        raise ValueError(message)
    if outcome.publication is AttemptPublication.PUBLISHED:
        if outcome.review_status is None:
            message = "published outcomes require one trusted review result"
            raise ValueError(message)
    elif outcome.review_status is not None:
        message = "unpublished outcomes cannot carry a review result"
        raise ValueError(message)


class AttemptServices(Protocol):
    """External resources owned by one child attempt."""

    def review(self, request: ReviewRequest) -> ReviewResult: ...

    def publish(self, request: ReviewRequest, result: ReviewResult) -> None: ...

    def close(self) -> None: ...


AttemptServicesFactory = Callable[[AttemptCommand, AttemptSettings], AttemptServices]


logger = logging.getLogger(__name__)


def _close_attempt_resources(
    *,
    resource_manager: ReviewResourceManager,
    attempt_id: str,
    github: GitHubAppClient,
) -> None:
    failure: Exception | None = None
    try:
        resource_manager.cleanup(attempt_id)
    except Exception as error:  # noqa: BLE001 - exact-cleanup failure boundary.
        failure = error
    try:
        github.close()
    except Exception as error:  # noqa: BLE001 - client-close failure boundary.
        if failure is None:
            failure = error
    if failure is not None:
        raise failure


class _ProductionAttemptServices:
    def __init__(
        self,
        *,
        command: AttemptCommand,
        settings: AttemptSettings,
    ) -> None:
        runtime = settings.runtime
        sandbox_client = DockerSandboxClient(config=runtime.sandbox_operation)
        resource_manager = ReviewResourceManager(
            workspace_root=settings.workspace_root,
            sandbox_prefix=runtime.sandbox_name_prefix,
            sandbox_client=sandbox_client,
        )
        resources = resource_manager.for_attempt(command.attempt_id)
        github = GitHubAppClient(
            repository=command.request.repository,
            app_id=settings.app_id,
            private_key_path=settings.private_key_path,
        )
        try:
            adapter = CodexSandboxAdapter(
                client=sandbox_client,
                resources=resources,
                kit=settings.review_kit_path,
                config=runtime.codex_execution,
            )
            candidate_acceptance = CandidateAcceptance(
                adapter=adapter,
                max_bytes=runtime.candidate_output_max_bytes,
            )
            reviewer = Reviewer(
                repository=command.request.repository,
                resources=resources,
                candidate_acceptance=candidate_acceptance,
                source_repository=GitHubRepository(credentials=github),
                limits=runtime.review_limits,
            )
        except BaseException:
            try:
                _close_attempt_resources(
                    resource_manager=resource_manager,
                    attempt_id=command.attempt_id,
                    github=github,
                )
            except Exception as cleanup_error:  # noqa: BLE001 - construction rollback.
                cleanup_stage, cleanup_category = _failure_details(
                    cleanup_error,
                    stage="cleanup",
                )
                _log_failure(
                    attempt_id=command.attempt_id,
                    stage=cleanup_stage,
                    category=cleanup_category,
                )
            raise
        self._attempt_id = command.attempt_id
        self._resource_manager = resource_manager
        self._github = github
        self._reviewer = reviewer

    def review(self, request: ReviewRequest) -> ReviewResult:
        return self._reviewer.review(request)

    def publish(self, request: ReviewRequest, result: ReviewResult) -> None:
        publish_review_result(
            request=request,
            result=result,
            gateway=self._github,
        )

    def close(self) -> None:
        _close_attempt_resources(
            resource_manager=self._resource_manager,
            attempt_id=self._attempt_id,
            github=self._github,
        )


def _create_production_services(
    command: AttemptCommand,
    settings: AttemptSettings,
) -> AttemptServices:
    return _ProductionAttemptServices(command=command, settings=settings)


def _failure_details(error: BaseException, *, stage: str) -> tuple[str, FailureCategory]:
    if isinstance(error, ReviewError):
        return error.stage, error.category
    if isinstance(error, TimeoutError):
        return stage, FailureCategory.TIMEOUT
    return stage, FailureCategory.REVIEW_FAILURE


def _log_failure(
    *,
    attempt_id: str,
    stage: str,
    category: FailureCategory,
) -> None:
    logger.warning(
        "review attempt failed attempt_id=%s stage=%s category=%s",
        attempt_id,
        stage,
        category.value,
    )


def _emit_outcome(command: AttemptCommand, outcome: AttemptOutcome) -> None:
    if command.outcome_fd is None:
        return
    try:
        with os.fdopen(command.outcome_fd, "wb", closefd=True) as sink:
            sink.write(outcome.to_json_bytes())
            sink.flush()
    except OSError:
        _log_failure(
            attempt_id=command.attempt_id,
            stage="outcome",
            category=FailureCategory.REVIEW_FAILURE,
        )


def _incomplete_outcome(
    command: AttemptCommand,
    *,
    failure: tuple[str, FailureCategory],
    review_status: Literal["no_important_issues", "issues_found"] | None = None,
    publication: AttemptPublication = AttemptPublication.NOT_ATTEMPTED,
) -> AttemptOutcome:
    stage, category = failure
    status = (
        AttemptStatus.TIMED_OUT
        if category is FailureCategory.TIMEOUT
        else AttemptStatus.FAILED
    )
    return AttemptOutcome(
        attempt_id=command.attempt_id,
        status=status,
        review_status=review_status,
        publication=publication,
        failure_stage=stage,
        failure_category=category,
    )


def _execute_configured_attempt(
    command: AttemptCommand,
    settings: AttemptSettings,
    *,
    services_factory: AttemptServicesFactory,
) -> AttemptOutcome:
    deadline = ReviewDeadline.after(settings.runtime.review_timeout_seconds)
    services: AttemptServices | None = None
    failure: tuple[str, FailureCategory] | None = None
    review_status: Literal["no_important_issues", "issues_found"] | None = None
    publication = AttemptPublication.NOT_ATTEMPTED
    stage = "attempt_construction"
    with review_deadline_scope(deadline):
        try:
            deadline.remaining(stage=stage)
            services = services_factory(command, settings)
            stage = "review"
            deadline.remaining(stage=stage)
            result = services.review(command.request)
            review_status = result.status
            deadline.remaining(stage=stage)
            stage = "publication"
            deadline.remaining(stage=stage)
            services.publish(command.request, result)
            publication = AttemptPublication.PUBLISHED
            deadline.remaining(stage=stage)
        except Exception as error:  # noqa: BLE001 - child attempt isolation boundary.
            failure = _failure_details(error, stage=stage)
        finally:
            if services is not None:
                failure = _close_services(
                    services,
                    deadline=deadline,
                    attempt_id=command.attempt_id,
                    existing_failure=failure,
                )

    if failure is not None:
        published_status = (
            review_status if publication is AttemptPublication.PUBLISHED else None
        )
        return _incomplete_outcome(
            command,
            failure=failure,
            review_status=published_status,
            publication=publication,
        )
    if review_status is None:
        return _incomplete_outcome(
            command,
            failure=("review", FailureCategory.REVIEW_FAILURE),
        )
    return AttemptOutcome(
        attempt_id=command.attempt_id,
        status=AttemptStatus.REVIEWED,
        review_status=review_status,
        publication=AttemptPublication.PUBLISHED,
        failure_stage=None,
        failure_category=None,
    )


def _close_services(
    services: AttemptServices,
    *,
    deadline: ReviewDeadline,
    attempt_id: str,
    existing_failure: tuple[str, FailureCategory] | None,
) -> tuple[str, FailureCategory] | None:
    try:
        services.close()
        deadline.remaining(stage="cleanup")
    except Exception as error:  # noqa: BLE001 - child cleanup boundary.
        cleanup_failure = _failure_details(error, stage="cleanup")
        if existing_failure is None:
            return cleanup_failure
        _log_failure(
            attempt_id=attempt_id,
            stage=cleanup_failure[0],
            category=cleanup_failure[1],
        )
    return existing_failure


def run_attempt_child(
    *,
    stdin: BinaryIO | None = None,
    environment: Mapping[str, str] | None = None,
    services_factory: AttemptServicesFactory | None = None,
) -> int:
    """Run one complete attempt from the child process interface."""
    source = sys.stdin.buffer if stdin is None else stdin
    resolved_environment = os.environ if environment is None else environment
    attempt_id = "unknown"
    try:
        command = AttemptCommand.from_json_bytes(source.read(ATTEMPT_COMMAND_MAX_BYTES + 1))
    except Exception:  # noqa: BLE001 - child launch-contract boundary.
        _log_failure(
            attempt_id=attempt_id,
            stage="launch_command",
            category=FailureCategory.REVIEW_FAILURE,
        )
        return 1

    attempt_id = command.attempt_id
    try:
        settings = AttemptSettings.from_environment(resolved_environment)
    except Exception:  # noqa: BLE001 - child configuration boundary.
        launch_failure = ("launch_configuration", FailureCategory.REVIEW_FAILURE)
        _log_failure(
            attempt_id=attempt_id,
            stage=launch_failure[0],
            category=launch_failure[1],
        )
        _emit_outcome(command, _incomplete_outcome(command, failure=launch_failure))
        return 1

    outcome = _execute_configured_attempt(
        command,
        settings,
        services_factory=services_factory or _create_production_services,
    )
    if outcome.failure_stage is not None and outcome.failure_category is not None:
        _log_failure(
            attempt_id=attempt_id,
            stage=outcome.failure_stage,
            category=outcome.failure_category,
        )
    _emit_outcome(command, outcome)
    return 0 if outcome.status is AttemptStatus.REVIEWED else 1


def main() -> int:
    """Run the production child-attempt process."""
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 1:
        _log_failure(
            attempt_id="unknown",
            stage="launch_command",
            category=FailureCategory.REVIEW_FAILURE,
        )
        return 1
    return run_attempt_child()


if __name__ == "__main__":
    raise SystemExit(main())
