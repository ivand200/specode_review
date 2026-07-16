from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{40}$")]
DESCRIPTION_MAX_CHARS = 10_000
DESCRIPTION_TRUNCATION_MARKER = "\n\n[truncated]"
RepositoryName = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=140,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    ),
]


def bound_description(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        message = "pull request description must be a string or null"
        raise TypeError(message)
    if len(value) <= DESCRIPTION_MAX_CHARS:
        return value
    prefix_length = DESCRIPTION_MAX_CHARS - len(DESCRIPTION_TRUNCATION_MARKER)
    return f"{value[:prefix_length]}{DESCRIPTION_TRUNCATION_MARKER}"


class ReviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    pr_number: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=DESCRIPTION_MAX_CHARS)

    @field_validator("description", mode="before")
    @classmethod
    def truncate_description(cls, value: object) -> str:
        return bound_description(value)


class DiffRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_sha: Sha
    end_sha: Sha


class Location(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, gt=0)
    description: str | None = Field(default=None, min_length=1, max_length=240)


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Literal["blocking", "important"]
    title: str = Field(min_length=1, max_length=160)
    locations: tuple[Location, ...] = Field(min_length=1, max_length=3)
    evidence: str = Field(min_length=1, max_length=1_200)
    impact: str = Field(min_length=1, max_length=600)
    suggested_fix: str = Field(min_length=1, max_length=600)


class AgentReview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[Finding, ...] = Field(max_length=5)


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    pr_number: int = Field(gt=0)
    diff_range: DiffRange
    status: Literal["issues_found", "no_important_issues"]
    findings: tuple[Finding, ...] = Field(max_length=5)

    @model_validator(mode="after")
    def status_matches_findings(self) -> "ReviewResult":
        expected_status = "issues_found" if self.findings else "no_important_issues"
        if self.status != expected_status:
            msg = "status must be derived from whether findings exist"
            raise ValueError(msg)
        return self
