from enum import StrEnum


class FailureCategory(StrEnum):
    REPOSITORY_MATERIALIZATION = "repository_materialization"
    REVIEW_TOO_LARGE = "review_too_large"
    SANDBOX_LIFECYCLE = "sandbox_lifecycle"
    CODEX_OR_LIMIT = "codex_or_limit"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    TIMEOUT = "timeout"
    REVIEW_FAILURE = "review_failure"


class ReviewError(Exception):
    def __init__(self, category: FailureCategory, *, stage: str) -> None:
        self.category = category
        self.stage = stage
        super().__init__(f"{category.value} during {stage}")
