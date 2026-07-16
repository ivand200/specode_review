from pathlib import Path


def test_example_environment_does_not_claim_unsupported_model_or_tool_limits() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "MODEL_REQUEST_LIMIT" not in example
    assert "TOOL_CALL_LIMIT" not in example
    assert "TOTAL_TOKEN_LIMIT" not in example
    assert "COUNT_INPUT_TOKENS_BEFORE_REQUEST" not in example
    assert "OPENAI_API_KEY" not in example
    assert "100 changed files" in example
    assert "5,000 changed text lines" in example
    assert "65,536 candidate JSON bytes" in example
