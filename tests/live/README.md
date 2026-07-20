# Live verification

The former Check Run and retry profiles were removed with the durable workflow.

The surviving live verification contract is comment-only: prepare a fresh exact base/head
revision, deliver a normally signed eligible pull-request webhook, require exactly one
application-owned exact-revision comment containing the seeded finding, and confirm that no owned
Sandbox or workspace remains. The installed `specode-review-real-e2e` command reserves that
purpose-specific entry point and reports that the campaign is unavailable until the complete
production-path profile is implemented.

Until then, use the deterministic network-free gates:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

The opt-in no-model Docker Sandbox capability probe remains available separately:

```bash
uv run pytest tests/integration/test_no_model_sandbox_probe.py -q -s
```
