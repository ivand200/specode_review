import sys

from review_agent.attempt import AttemptCommand, AttemptServices, run_attempt_child
from review_agent.configuration import AttemptSettings
from review_agent.deadline import remaining_review_time
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import DiffRange, ReviewRequest, ReviewResult


def _record(event: str) -> None:
    sys.stdout.write(f"{event}\n")
    sys.stdout.flush()


class FixtureServices:
    def __init__(self, mode: str) -> None:
        self._mode = mode

    def review(self, request: ReviewRequest) -> ReviewResult:
        assert remaining_review_time(stage="fixture_review") is not None
        _record("review")
        if self._mode == "review_failure":
            raise ReviewError(
                FailureCategory.REVIEW_TOO_LARGE,
                stage="review_size",
            )
        if self._mode == "validation_failure":
            raise ReviewError(
                FailureCategory.INVALID_MODEL_OUTPUT,
                stage="candidate_validation",
            )
        if self._mode == "timeout":
            message = "secret timeout with model text and subprocess output"
            raise TimeoutError(message)
        return ReviewResult(
            repository=request.repository,
            pr_number=request.pr_number,
            diff_range=DiffRange(
                start_sha=request.base_sha,
                end_sha=request.head_sha,
            ),
            status="no_important_issues",
            findings=(),
        )

    def publish(self, result: ReviewResult, *, installation_id: int) -> None:
        del result, installation_id
        assert remaining_review_time(stage="fixture_publication") is not None
        _record("publication")
        if self._mode == "publication_failure":
            message = "secret publication exception with token and rendered model comment"
            raise RuntimeError(message)

    def close(self) -> None:
        assert remaining_review_time(stage="fixture_cleanup") is not None
        _record("cleanup")
        if self._mode == "cleanup_failure":
            message = "secret cleanup exception with subprocess output"
            raise RuntimeError(message)


def create_fixture_services(
    command: AttemptCommand,
    settings: AttemptSettings,
) -> AttemptServices:
    del command, settings
    mode = sys.argv[1] if len(sys.argv) > 1 else "success"
    if mode == "construction_failure":
        message = "secret construction exception with private key"
        raise RuntimeError(message)
    return FixtureServices(mode)


raise SystemExit(run_attempt_child(services_factory=create_fixture_services))
