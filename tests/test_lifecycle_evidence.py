import asyncio
import json
import logging

import pytest

from review_agent import ReviewLifecycle
from review_agent.errors import FailureCategory, ReviewError
from review_agent.models import ReviewRequest
from review_agent.publishing import PublicationDisposition
from review_agent.review_runner import PreflightOutcome, ReviewCompletion
from review_agent.submission import SubmissionOutcome


def _request(**updates: object) -> ReviewRequest:
    return ReviewRequest(
        repository="Octo-Org/Example",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="sentinel title that must never be logged",
        description="sentinel description that must never be logged",
    ).model_copy(update=updates)


class SuccessfulRunner:
    def preflight(self, _request: ReviewRequest) -> PreflightOutcome:
        return PreflightOutcome.READY

    def run(self, _request: ReviewRequest, _attempt_id: str) -> ReviewCompletion:
        return ReviewCompletion(
            review_status="no_important_issues",
            finding_count=0,
            publication=PublicationDisposition.CREATED,
            comment_id=91,
        )


class FailingPreflightRunner:
    def preflight(self, _request: ReviewRequest) -> PreflightOutcome:
        message = "unexpected provider response with sentinel-secret"
        raise RuntimeError(message)

    def run(self, _request: ReviewRequest, _attempt_id: str) -> object:
        message = "unavailable preflight must not run a review"
        raise AssertionError(message)


class FailingReviewRunner(SuccessfulRunner):
    def run(self, _request: ReviewRequest, _attempt_id: str) -> ReviewCompletion:
        raise ReviewError(
            FailureCategory.INVALID_MODEL_OUTPUT,
            stage="candidate_validation",
        )


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "review_agent.lifecycle_evidence"
    ]


def test_lifecycle_emits_correlated_single_line_json_for_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> SubmissionOutcome:
        lifecycle = ReviewLifecycle(
            runner=SuccessfulRunner(),
            attempt_id_factory=lambda: "attempt-123",
        )
        async with lifecycle:
            return await lifecycle.submit(_request())

    caplog.set_level(logging.INFO, logger="review_agent.lifecycle_evidence")

    assert asyncio.run(exercise()) is SubmissionOutcome.ACCEPTED

    records = _records(caplog)
    assert [record["event"] for record in records] == [
        "preflight",
        "running",
        "admission",
        "terminal_release",
    ]
    assert all(record["repository"] == "octo-org/example" for record in records)
    assert all(record["pull_request"] == 17 for record in records)
    assert all(record["accepted_revision"] == "b" * 40 for record in records)
    assert records[0]["terminal_outcome"] == "ready"
    assert records[1]["attempt_id"] == "attempt-123"
    assert records[2]["admission_disposition"] == "accepted"
    assert records[3]["terminal_outcome"] == "succeeded"
    assert records[3]["publication_disposition"] == "created"
    assert isinstance(records[3]["duration_ms"], int)
    assert all("\n" not in record.getMessage() for record in caplog.records)
    assert "sentinel" not in caplog.text


def test_unexpected_preflight_failure_uses_only_safe_vocabulary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> SubmissionOutcome:
        async with ReviewLifecycle(runner=FailingPreflightRunner()) as lifecycle:
            return await lifecycle.submit(_request())

    caplog.set_level(logging.INFO, logger="review_agent.lifecycle_evidence")

    assert asyncio.run(exercise()) is SubmissionOutcome.UNAVAILABLE

    records = _records(caplog)
    assert [record["event"] for record in records] == [
        "normalized_failure",
        "preflight",
        "admission",
    ]
    assert records[0]["stage"] == "preflight"
    assert records[0]["category"] == "review_failure"
    assert records[1]["terminal_outcome"] == "unavailable"
    assert records[2]["admission_disposition"] == "unavailable"
    assert "sentinel-secret" not in caplog.text


def test_review_failure_is_normalized_and_terminally_released(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> SubmissionOutcome:
        async with ReviewLifecycle(
            runner=FailingReviewRunner(),
            attempt_id_factory=lambda: "attempt-789",
        ) as lifecycle:
            return await lifecycle.submit(_request())

    caplog.set_level(logging.INFO, logger="review_agent.lifecycle_evidence")

    assert asyncio.run(exercise()) is SubmissionOutcome.ACCEPTED

    records = _records(caplog)
    assert [record["event"] for record in records] == [
        "preflight",
        "running",
        "admission",
        "normalized_failure",
        "terminal_release",
    ]
    assert records[3]["attempt_id"] == "attempt-789"
    assert records[3]["stage"] == "candidate_validation"
    assert records[3]["category"] == "invalid_model_output"
    assert records[4]["terminal_outcome"] == "failed"
