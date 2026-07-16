# Live GitHub profile

This opt-in checkpoint starts the FastAPI service on a real local socket, sends a signed
GitHub-shaped webhook for one real pull request, and uses the configured GitHub App to
clone the base repository, fetch `refs/pull/<number>/head`, review the exact accepted head
with the deterministic clean runner, and create one real top-level pull-request comment.
It does not use Docker Sandboxes, OpenAI authentication, or model budget.

The repository name must contain `test`, and the explicit E2E repository must equal the
application's configured repository. Normal `pytest` runs skip this profile.

Run it only against a disposable, open, non-draft pull request:

```bash
set -a
source .env
set +a
RUN_LIVE_GITHUB_E2E=1 \
E2E_GITHUB_REPOSITORY=ivand200/test_repo \
E2E_GITHUB_PR_NUMBER=<number> \
E2E_CREATED_RESOURCES_PATH=/tmp/review-agent-live-resources.jsonl \
uv run pytest tests/live/test_github_live.py -q
```

The profile leaves the created review comment in the test pull request as its observable
result and appends its repository, pull-request number, and cleanup instruction to
`E2E_CREATED_RESOURCES_PATH`. Remove the recorded comment manually when it is no longer
useful. Abrupt termination can occur after GitHub creates the comment but before the
cleanup record is written, so inspect the dedicated pull request after any interrupted run.
