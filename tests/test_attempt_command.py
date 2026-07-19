import pytest
from pydantic import ValidationError

from review_agent.attempt import (
    ATTEMPT_COMMAND_MAX_BYTES,
    ATTEMPT_OUTCOME_MAX_BYTES,
    AttemptCommand,
    AttemptCommandError,
    AttemptOutcome,
    AttemptOutcomeError,
    AttemptPublication,
    AttemptStatus,
)
from review_agent.errors import FailureCategory
from review_agent.models import DESCRIPTION_MAX_CHARS, ReviewRequest


def _request() -> ReviewRequest:
    return ReviewRequest(
        repository="octo-org/review-fixture",
        pr_number=17,
        installation_id=23,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Review the accepted revision",
        description="Bounded pull request description",
    )


def test_attempt_command_round_trips_one_closed_json_document() -> None:
    command = AttemptCommand(
        attempt_id="0123456789abcdef0123456789abcdef",
        check_run_id=101,
        outcome_fd=9,
        request=_request(),
    )

    parsed = AttemptCommand.from_json_bytes(command.to_json_bytes())

    assert parsed == command
    assert parsed.request is not command.request
    with pytest.raises(ValidationError, match="frozen"):
        parsed.attempt_id = "f" * 32


@pytest.mark.parametrize(
    ("check_run_id", "outcome_fd"),
    [
        (0, 9),
        (-1, 9),
        (True, 9),
        (101, 0),
        (101, 2),
        (101, True),
        (101, None),
        (None, 9),
    ],
)
def test_attempt_command_rejects_invalid_check_run_or_outcome_channel(
    check_run_id: object,
    outcome_fd: object,
) -> None:
    with pytest.raises(ValidationError):
        AttemptCommand(
            attempt_id="0123456789abcdef0123456789abcdef",
            check_run_id=check_run_id,
            outcome_fd=outcome_fd,
            request=_request(),
        )


def test_attempt_outcome_round_trips_a_trusted_published_review() -> None:
    outcome = AttemptOutcome(
        attempt_id="0123456789abcdef0123456789abcdef",
        status=AttemptStatus.REVIEWED,
        review_status="issues_found",
        publication=AttemptPublication.PUBLISHED,
        failure_stage=None,
        failure_category=None,
    )

    assert AttemptOutcome.from_json_bytes(
        outcome.to_json_bytes(),
        expected_attempt_id=outcome.attempt_id,
    ) == outcome


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(
            {
                "status": "reviewed",
                "review_status": None,
                "publication": "published",
                "failure_stage": None,
                "failure_category": None,
            },
            id="reviewed-without-review-status",
        ),
        pytest.param(
            {
                "status": "reviewed",
                "review_status": "no_important_issues",
                "publication": "not_attempted",
                "failure_stage": None,
                "failure_category": None,
            },
            id="reviewed-without-publication",
        ),
        pytest.param(
            {
                "status": "failed",
                "review_status": None,
                "publication": "not_attempted",
                "failure_stage": None,
                "failure_category": "review_failure",
            },
            id="failed-without-stage",
        ),
        pytest.param(
            {
                "status": "timed_out",
                "review_status": None,
                "publication": "unknown",
                "failure_stage": "timeout",
                "failure_category": "review_failure",
            },
            id="timeout-with-wrong-category",
        ),
        pytest.param(
            {
                "status": "failed",
                "review_status": None,
                "publication": "published",
                "failure_stage": "cleanup",
                "failure_category": "review_failure",
            },
            id="published-without-trusted-review",
        ),
    ],
)
def test_attempt_outcome_rejects_invalid_field_combinations(
    outcome: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AttemptOutcome(
            attempt_id="0123456789abcdef0123456789abcdef",
            **outcome,
        )


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"not-json",
        b"{}",
        (
            b'{"attempt_id":"0123456789abcdef0123456789abcdef","status":"failed",'
            b'"review_status":null,"publication":"not_attempted","failure_stage":"review",'
            b'"failure_category":"review_failure","extra":"unsafe"}'
        ),
        (
            b'{"attempt_id":"0123456789abcdef0123456789abcdef","status":"failed",'
            b'"review_status":null,"publication":"not_attempted","failure_stage":"review",'
            b'"failure_category":"review_failure"}{}'
        ),
    ],
)
def test_attempt_outcome_rejects_incomplete_unknown_malformed_or_duplicated_input(
    document: bytes,
) -> None:
    with pytest.raises(AttemptOutcomeError, match="invalid attempt outcome"):
        AttemptOutcome.from_json_bytes(
            document,
            expected_attempt_id="0123456789abcdef0123456789abcdef",
        )


def test_attempt_outcome_rejects_oversized_or_mismatched_input() -> None:
    valid = AttemptOutcome(
        attempt_id="0123456789abcdef0123456789abcdef",
        status=AttemptStatus.FAILED,
        review_status=None,
        publication=AttemptPublication.NOT_ATTEMPTED,
        failure_stage="review",
        failure_category=FailureCategory.REVIEW_FAILURE,
    )

    with pytest.raises(AttemptOutcomeError, match="invalid attempt outcome"):
        AttemptOutcome.from_json_bytes(
            b"x" * (ATTEMPT_OUTCOME_MAX_BYTES + 1),
            expected_attempt_id=valid.attempt_id,
        )
    with pytest.raises(AttemptOutcomeError, match="invalid attempt outcome"):
        AttemptOutcome.from_json_bytes(
            valid.to_json_bytes(),
            expected_attempt_id="f" * 32,
        )


@pytest.mark.parametrize(
    "attempt_id",
    [
        "",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "g" * 32,
        "../" + "a" * 29,
    ],
)
def test_attempt_command_rejects_malformed_attempt_id(attempt_id: str) -> None:
    with pytest.raises(ValidationError):
        AttemptCommand(attempt_id=attempt_id, request=_request())


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"not-json",
        b"{}",
        b'{"attempt_id":"0123456789abcdef0123456789abcdef"}',
        b'{"attempt_id":"0123456789abcdef0123456789abcdef","request":{},"extra":1}',
        b'{"attempt_id":"0123456789abcdef0123456789abcdef","request":{}}{}',
        (
            b'{"attempt_id":"0123456789abcdef0123456789abcdef","request":{'
            b'"repository":"octo-org/review-fixture","pr_number":"17",'
            b'"installation_id":23,"base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"head_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            b'"title":"wrong JSON type"}}'
        ),
    ],
)
def test_attempt_command_rejects_incomplete_unknown_malformed_or_trailing_input(
    document: bytes,
) -> None:
    with pytest.raises(AttemptCommandError, match="invalid attempt command") as failure:
        AttemptCommand.from_json_bytes(document)

    if document:
        assert document.decode(errors="ignore") not in str(failure.value)


def test_attempt_command_rejects_input_before_parsing_when_byte_limit_is_exceeded() -> None:
    document = b"sensitive-pull-request-content" * (
        ATTEMPT_COMMAND_MAX_BYTES // len(b"sensitive-pull-request-content") + 1
    )

    with pytest.raises(AttemptCommandError, match="invalid attempt command") as failure:
        AttemptCommand.from_json_bytes(document)

    assert len(document) > ATTEMPT_COMMAND_MAX_BYTES
    assert "sensitive-pull-request-content" not in str(failure.value)


def test_attempt_command_accepts_the_largest_valid_multibyte_review_request() -> None:
    request = _request().model_copy(
        update={
            "title": "🧪" * 256,
            "description": "🔒" * DESCRIPTION_MAX_CHARS,
        }
    )
    command = AttemptCommand(
        attempt_id="0123456789abcdef0123456789abcdef",
        check_run_id=101,
        outcome_fd=9,
        request=request,
    )

    document = command.to_json_bytes()

    assert len(document) <= ATTEMPT_COMMAND_MAX_BYTES
    assert AttemptCommand.from_json_bytes(document) == command
