import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from review_agent import (
    AgentReview,
    CandidateAcceptance,
    ChangedPathManifest,
    DiffRange,
    FailureCategory,
    ReviewContext,
    ReviewError,
    ReviewRequest,
    SandboxResourceLimits,
)
from review_agent.core import CandidateContract


class RecordingCandidateAdapter:
    def __init__(self, candidate: object) -> None:
        self.candidate = candidate
        self.calls: list[tuple[ReviewContext, CandidateContract]] = []

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        self.calls.append((context, contract))
        return self.candidate  # type: ignore[return-value]


class RaisingCandidateAdapter:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def produce(
        self,
        context: ReviewContext,
        contract: CandidateContract,
    ) -> bytes:
        del context, contract
        raise self.error


class SweepingCandidateAdapter(RecordingCandidateAdapter):
    def __init__(self) -> None:
        super().__init__(b'{"findings":[]}')
        self.sweeps = 0

    def sweep_orphans(self) -> None:
        self.sweeps += 1


def _context(tmp_path: Path) -> ReviewContext:
    tmp_path.mkdir(parents=True, exist_ok=True)
    start_sha = "a" * 40
    head_sha = "b" * 40
    request = ReviewRequest(
        repository="octo-org/example",
        pr_number=17,
        installation_id=23,
        base_sha=start_sha,
        head_sha=head_sha,
        title="Review the change",
    )
    diff_range = DiffRange(start_sha=start_sha, end_sha=head_sha)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    return ReviewContext(
        request=request,
        workspace=tmp_path,
        checkout=checkout,
        diff_range=diff_range,
        manifest=ChangedPathManifest(
            diff_range=diff_range,
            paths=("feature.txt",),
            changed_files=1,
            changed_text_lines=1,
        ),
        sandbox_resources=SandboxResourceLimits(),
    )


def test_accept_delivers_one_immutable_contract_and_accepts_zero_findings(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    adapter = RecordingCandidateAdapter(b'{"findings":[]}')
    acceptance = CandidateAcceptance(adapter=adapter, max_bytes=1_024)

    candidate = acceptance.accept(context)

    assert candidate == AgentReview(findings=())
    assert len(adapter.calls) == 1
    called_context, contract = adapter.calls[0]
    assert called_context is context
    assert contract.max_bytes == 1_024
    with pytest.raises(FrozenInstanceError):
        contract.max_bytes = 2_048  # type: ignore[misc]
    expected_schema_json = json.dumps(
        AgentReview.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert contract.schema_json == expected_schema_json
    assert (
        json.dumps(
            json.loads(contract.schema_json),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        == contract.schema_json
    )
    schema = json.loads(contract.schema_json)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"findings"}
    location_schema = schema["$defs"]["Location"]
    assert location_schema["additionalProperties"] is False
    assert set(location_schema["required"]) == {"path", "line", "description"}


def test_accept_rechecks_the_exact_contract_byte_budget(tmp_path: Path) -> None:
    context = _context(tmp_path)
    raw_candidate = b'{"findings":[]}'

    exact = CandidateAcceptance(
        adapter=RecordingCandidateAdapter(raw_candidate),
        max_bytes=len(raw_candidate),
    ).accept(context)

    assert exact == AgentReview(findings=())
    with pytest.raises(ReviewError) as failure:
        CandidateAcceptance(
            adapter=RecordingCandidateAdapter(raw_candidate + b" "),
            max_bytes=len(raw_candidate),
        ).accept(context)
    assert failure.value.category is FailureCategory.CODEX_OR_LIMIT
    assert failure.value.stage == "candidate_output"


@pytest.mark.parametrize(
    "candidate",
    [
        b"not-json",
        (
            b'{"findings":[{"severity":"important","title":"Invalid",'
            b'"locations":[{"path":"feature.txt"}],"evidence":"Evidence",'
            b'"impact":"Impact","suggested_fix":"Fix"}]}'
        ),
        (
            b'{"findings":[{"severity":"important","title":"Invalid",'
            b'"locations":[{"path":"feature.txt","line":"1","description":null}],'
            b'"evidence":"Evidence","impact":"Impact","suggested_fix":"Fix"}]}'
        ),
        (
            b'{"findings":[{"severity":"important","title":"Invalid",'
            b'"locations":[{"path":"feature.txt","line":1.0,"description":null}],'
            b'"evidence":"Evidence","impact":"Impact","suggested_fix":"Fix"}]}'
        ),
        b'{"findings":[],"unknown":true}',
    ],
    ids=["malformed", "missing-nullables", "numeric-string", "integral-float", "extra"],
)
def test_accept_rejects_malformed_or_non_strict_candidates(
    tmp_path: Path,
    candidate: bytes,
) -> None:
    context = _context(tmp_path)
    (context.checkout / "feature.txt").write_text("feature\n", encoding="utf-8")
    acceptance = CandidateAcceptance(
        adapter=RecordingCandidateAdapter(candidate),
        max_bytes=1_024,
    )

    with pytest.raises(ReviewError) as failure:
        acceptance.accept(context)

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert failure.value.stage == "candidate_validation"


@pytest.mark.parametrize(
    "candidate",
    [
        '{"findings":[]}',
        {"findings": []},
        AgentReview(findings=()),
        bytearray(b'{"findings":[]}'),
    ],
    ids=["string", "mapping", "model", "bytearray"],
)
def test_accept_rejects_every_non_bytes_transport_result(
    tmp_path: Path,
    candidate: object,
) -> None:
    acceptance = CandidateAcceptance(
        adapter=RecordingCandidateAdapter(candidate),
        max_bytes=1_024,
    )

    with pytest.raises(ReviewError) as failure:
        acceptance.accept(_context(tmp_path))

    assert failure.value.category is FailureCategory.INVALID_MODEL_OUTPUT
    assert failure.value.stage == "candidate_validation"


def test_accept_preserves_order_after_grounding_every_location(tmp_path: Path) -> None:
    context = _context(tmp_path)
    (context.checkout / "feature.txt").write_text("feature\n", encoding="utf-8")
    (context.checkout / "shared.txt").write_text("shared\n", encoding="utf-8")

    def finding(title: str) -> dict[str, object]:
        return {
            "severity": "important",
            "title": title,
            "locations": [
                {"path": "shared.txt", "line": 1, "description": None},
                {"path": "feature.txt", "line": 1, "description": None},
            ],
            "evidence": "Evidence",
            "impact": "Impact",
            "suggested_fix": "Fix",
        }

    candidate = json.dumps(
        {"findings": [finding("First"), finding("Second")]},
        separators=(",", ":"),
    ).encode()
    acceptance = CandidateAcceptance(
        adapter=RecordingCandidateAdapter(candidate),
        max_bytes=len(candidate),
    )

    accepted = acceptance.accept(context)

    assert tuple(item.title for item in accepted.findings) == ("First", "Second")


def test_acceptance_construction_sweeps_optional_adapter_orphans() -> None:
    adapter = SweepingCandidateAdapter()

    CandidateAcceptance(adapter=adapter, max_bytes=1_024)

    assert adapter.sweeps == 1


def test_acceptance_construction_rejects_a_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="candidate output limit must be positive"):
        CandidateAcceptance(
            adapter=RecordingCandidateAdapter(b'{"findings":[]}'),
            max_bytes=0,
        )


def test_acceptance_construction_fails_visibly_on_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        AgentReview,
        "model_json_schema",
        classmethod(
            lambda _cls: {
                "type": "object",
                "properties": {"findings": {"type": "array"}},
                "required": [],
                "additionalProperties": True,
            }
        ),
    )

    with pytest.raises(RuntimeError, match="schema object invariants"):
        CandidateAcceptance(
            adapter=RecordingCandidateAdapter(b'{"findings":[]}'),
            max_bytes=1_024,
        )


def test_accept_normalizes_only_adapter_timeouts(tmp_path: Path) -> None:
    timeout = CandidateAcceptance(
        adapter=RaisingCandidateAdapter(TimeoutError()),
        max_bytes=1_024,
    )

    with pytest.raises(ReviewError) as failure:
        timeout.accept(_context(tmp_path))

    assert failure.value.category is FailureCategory.TIMEOUT
    assert failure.value.stage == "review_runner"


def test_accept_preserves_review_errors_and_unexpected_adapter_errors(
    tmp_path: Path,
) -> None:
    normalized = ReviewError(FailureCategory.SANDBOX_LIFECYCLE, stage="sandbox_create")
    with pytest.raises(ReviewError) as review_failure:
        CandidateAcceptance(
            adapter=RaisingCandidateAdapter(normalized),
            max_bytes=1_024,
        ).accept(_context(tmp_path))
    assert review_failure.value is normalized

    programming_error = RuntimeError("adapter defect")
    with pytest.raises(RuntimeError) as unexpected_failure:
        CandidateAcceptance(
            adapter=RaisingCandidateAdapter(programming_error),
            max_bytes=1_024,
        ).accept(_context(tmp_path / "unexpected"))
    assert unexpected_failure.value is programming_error
