import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_validator

from specode_review.models import RepositoryName, ReviewRequest, Sha


class AcceptedRevision(BaseModel):
    """Canonical identity of one accepted pull-request revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    pr_number: int = Field(gt=0)
    base_sha: Sha
    head_sha: Sha

    @field_validator("repository", "base_sha", "head_sha", mode="before")
    @classmethod
    def normalize_text_identity(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @classmethod
    def from_review_request(cls, request: ReviewRequest) -> "AcceptedRevision":
        return cls(
            repository=request.repository,
            pr_number=request.pr_number,
            base_sha=request.base_sha,
            head_sha=request.head_sha,
        )

    @property
    def external_id(self) -> str:
        canonical = f"v1\n{self.repository}\n{self.pr_number}\n{self.base_sha}\n{self.head_sha}"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return f"specode-review:v1:{digest}"
