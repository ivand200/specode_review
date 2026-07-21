# Live verification

The former Check Run and retry profiles were removed with the durable workflow.

The surviving live verification contract is comment-only: prepare a fresh exact base/head
revision in a dedicated test repository, then run the installed `specode-review-real-e2e`
command. It closes and reopens that PR so GitHub delivers one normally signed eligible event
through the configured public endpoint. The command requires exactly one application-owned
exact-revision comment containing the seeded finding at the expected changed path and line,
correlated safe lifecycle logs, and no owned Sandbox or workspace.

Before the live campaign, run the deterministic network-free gates:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

The opt-in no-model Docker Sandbox capability probe remains available separately:

```bash
uv run pytest tests/integration/test_no_model_sandbox_probe.py -q -s
```

See the root `README.md` “Signed production release campaign” section for prerequisites, bounded
external effects, interruption recovery, manual cleanup, and evidence interpretation.
