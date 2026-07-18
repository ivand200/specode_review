import logging
import os
import sys
from collections.abc import Callable, Mapping
from typing import Annotated, BinaryIO, Protocol, Self

from pydantic import BaseModel, ConfigDict, StringConstraints

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

AttemptId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
]


class AttemptCommandError(ValueError):
    """A normalized, payload-safe launch-contract failure."""

    def __init__(self) -> None:
        super().__init__("invalid attempt command")


class AttemptCommand(BaseModel):
    """The complete immutable command delivered to one review child."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: AttemptId
    request: ReviewRequest

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


class AttemptServices(Protocol):
    """External resources owned by one child attempt."""

    def review(self, request: ReviewRequest) -> ReviewResult: ...

    def publish(self, result: ReviewResult, *, installation_id: int) -> None: ...

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

    def publish(self, result: ReviewResult, *, installation_id: int) -> None:
        publish_review_result(
            result,
            self._github,
            installation_id=installation_id,
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
        _log_failure(
            attempt_id=attempt_id,
            stage="launch_configuration",
            category=FailureCategory.REVIEW_FAILURE,
        )
        return 1

    deadline = ReviewDeadline.after(settings.runtime.review_timeout_seconds)
    resolved_services_factory = services_factory or _create_production_services
    services: AttemptServices | None = None
    failure: tuple[str, FailureCategory] | None = None
    stage = "attempt_construction"
    with review_deadline_scope(deadline):
        try:
            deadline.remaining(stage=stage)
            services = resolved_services_factory(command, settings)
            stage = "review"
            deadline.remaining(stage=stage)
            result = services.review(command.request)
            deadline.remaining(stage=stage)
            stage = "publication"
            deadline.remaining(stage=stage)
            services.publish(result, installation_id=command.request.installation_id)
            deadline.remaining(stage=stage)
        except Exception as error:  # noqa: BLE001 - child attempt isolation boundary.
            failure = _failure_details(error, stage=stage)
        finally:
            if services is not None:
                stage = "cleanup"
                try:
                    services.close()
                    deadline.remaining(stage=stage)
                except Exception as error:  # noqa: BLE001 - child cleanup boundary.
                    cleanup_failure = _failure_details(error, stage=stage)
                    if failure is None:
                        failure = cleanup_failure
                    else:
                        _log_failure(
                            attempt_id=attempt_id,
                            stage=cleanup_failure[0],
                            category=cleanup_failure[1],
                        )

    if failure is not None:
        _log_failure(
            attempt_id=attempt_id,
            stage=failure[0],
            category=failure[1],
        )
        return 1
    return 0


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
