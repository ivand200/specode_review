from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints

from review_agent.models import ReviewRequest

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
