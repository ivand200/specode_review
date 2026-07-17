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

## Docker Sandbox lifecycle profile

The no-model Docker profile creates disposable shell sandboxes, denies their outbound
network access, verifies the host checkout mount is read-only, copies it into writable VM
storage, checks the exact Git head, mutates the VM-local copy, and proves removal, timeout
cleanup, fresh sequential state, and strict orphan sweeping. It requires a working, signed-in
Docker Sandboxes runtime but no OpenAI authentication or model budget.

Run it explicitly with:

    RUN_DOCKER_SANDBOX_E2E=1 uv run pytest tests/integration/test_sandbox_lifecycle.py -q

## Full live sandboxed review (checkpoint C)

This cost-bearing rollout gate exercises the real signed webhook, queue, GitHub App credential
flow, exact Git checkout, Docker Sandbox, Codex CLI, grounding, deterministic publication, and
cleanup. It must target a disposable pull request in a dedicated repository whose name contains
`test`. Prepare that PR with a known important defect plus malicious repository-owned `AGENTS.md`
and `.codex/config.toml` instructions containing distinct markers; those files must remain
untrusted data. Set the expected finding text to a stable fragment describing the seeded defect.
The checkpoint verifies that the application kit is passed to the real sandbox, the Codex
invocation ignores repository configuration, and an in-sandbox request to `github.com` is denied
while the OpenAI-backed Codex run still succeeds.

The test is skipped unless both the live switch and cost acknowledgement are present:

```bash
set -a
source .env
set +a
RUN_FULL_LIVE_E2E=1 \
ACKNOWLEDGE_MODEL_COST=1 \
E2E_GITHUB_REPOSITORY=ivand200/test_repo \
E2E_GITHUB_PR_NUMBER=<number> \
E2E_EXPECTED_FINDING=<stable-defect-fragment> \
E2E_FORBIDDEN_REPOSITORY_INSTRUCTION_TEXT=<malicious-output-marker> \
E2E_FORBIDDEN_REPOSITORY_CONFIG_TEXT=<malicious-config-marker> \
E2E_CREATED_RESOURCES_PATH=/tmp/review-agent-full-live-resources.jsonl \
uv run pytest tests/live/test_full_live.py -q -s
```

The profile creates a real PR comment and records cleanup instructions. Inspect and remove that
comment afterward. A failure blocks production rollout. Never point this profile at an important
working copy or repository, and never run it without explicit approval for GitHub writes, model
cost, Docker Sandbox use, and the expected runtime duration.
