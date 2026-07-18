import pytest
from pydantic import ValidationError

from review_agent.attempt import (
    ATTEMPT_COMMAND_MAX_BYTES,
    AttemptCommand,
    AttemptCommandError,
)
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
        request=_request(),
    )

    parsed = AttemptCommand.from_json_bytes(command.to_json_bytes())

    assert parsed == command
    assert parsed.request is not command.request
    with pytest.raises(ValidationError, match="frozen"):
        parsed.attempt_id = "f" * 32


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
        request=request,
    )

    document = command.to_json_bytes()

    assert len(document) <= ATTEMPT_COMMAND_MAX_BYTES
    assert AttemptCommand.from_json_bytes(document) == command
