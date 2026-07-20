import json
import logging
import re
from typing import Any

from specode_review.errors import FailureCategory, ReviewError
from specode_review.models import ReviewRequest

logger = logging.getLogger(__name__)

_SAFE_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_FAILURE_STAGES = frozenset(
    {
        "attempt_construction",
        "candidate_grounding",
        "candidate_output",
        "candidate_validation",
        "client_cleanup",
        "cleanup",
        "codex_candidate_output",
        "codex_execution",
        "codex_output_limit",
        "codex_process_exit",
        "codex_sandbox_lifecycle",
        "git_configuration",
        "preflight",
        "publication",
        "publication_reconciliation",
        "repository_materialization",
        "review",
        "review_runner",
        "review_size",
        "sandbox_cleanup",
        "sandbox_create",
        "sandbox_execute",
        "sandbox_head_verification",
        "trusted_control_integrity",
        "workspace_allocation",
        "workspace_cleanup",
    }
)


def emit_lifecycle_evidence(
    request: ReviewRequest,
    event: str,
    *,
    attempt_id: str | None = None,
    **facts: str | int,
) -> None:
    """Emit bounded application-owned facts without review content."""
    record: dict[str, Any] = {
        "event": event,
        "repository": request.repository.lower(),
        "pull_request": request.pr_number,
        "accepted_revision": request.head_sha.lower(),
    }
    if attempt_id is not None:
        record["attempt_id"] = (
            attempt_id if _SAFE_ATTEMPT_ID.fullmatch(attempt_id) else "invalid"
        )
    record.update(facts)
    logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


def emit_normalized_failure(
    request: ReviewRequest,
    error: Exception,
    *,
    fallback_stage: str,
    attempt_id: str | None = None,
) -> None:
    category = (
        error.category
        if isinstance(error, ReviewError)
        else FailureCategory.TIMEOUT
        if isinstance(error, TimeoutError)
        else FailureCategory.REVIEW_FAILURE
    )
    candidate_stage = error.stage if isinstance(error, ReviewError) else fallback_stage
    stage = (
        candidate_stage if candidate_stage in _SAFE_FAILURE_STAGES else fallback_stage
    )
    emit_lifecycle_evidence(
        request,
        "normalized_failure",
        attempt_id=attempt_id,
        stage=stage,
        category=category.value,
    )
