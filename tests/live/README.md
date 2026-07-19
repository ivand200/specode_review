# Operational rollout profiles

All live profiles are opt-in and must target a disposable, open, non-draft pull request in a
dedicated repository whose name contains `test`. Normal `pytest` runs skip them. Use a fresh
accepted base/head revision: durable Check Run identity intentionally makes a repeated completed
run return `already_reviewed`.

## Checkpoint B: real GitHub lifecycle without a model

This profile starts the HTTP service on a real socket and sends signed webhooks through the public
interface. A controlled child first fails, allowing the test to verify a real neutral Check Run
and its `Retry review` action. It then sends a signed `check_run.requested_action`, verifies queued
and running states, a fresh attempt ID on the same Check Run, publishes a deterministic rendered
clean comment, and verifies `completed/success`.

It uses real GitHub writes but no Docker Sandbox, OpenAI authentication, or model budget:

```bash
set -a
source .env
set +a
RUN_LIVE_GITHUB_E2E=1 \
E2E_GITHUB_REPOSITORY=ivand200/test_repo \
E2E_GITHUB_PR_NUMBER=<number> \
E2E_CREATED_RESOURCES_PATH=/tmp/review-agent-live-resources.jsonl \
uv run pytest tests/live/test_github_live.py -q -s
```

The resource file records the Check Run ID, PR number, and cleanup instruction. Delete the
recorded automated review comment manually. GitHub Check Runs cannot be deleted through this
profile; retain it as rollout evidence. After interruption, inspect both the PR and its checks
because GitHub writes can succeed before the local cleanup record is appended.

## No-model Docker Sandbox lifecycle

This profile creates disposable shell Sandboxes, denies outbound network access, verifies the
host checkout mount is read-only, copies it into writable VM storage, checks the exact Git head,
mutates the VM-local copy, and proves removal, timeout cleanup, fresh sequential state, and strict
orphan sweeping:

```bash
RUN_DOCKER_SANDBOX_E2E=1 \
uv run pytest tests/integration/test_no_model_sandbox_probe.py -q -s
```

It needs a working, signed-in Docker Sandboxes runtime but no OpenAI credential or model budget.

## Checkpoint C: full production lifecycle

This cost-bearing gate uses the actual production assembly: persistent state validation,
repository lock, stale-resource sweep, GitHub installation lookup, signed webhook, durable Check
Run coordination, a real child outcome pipe, exact checkout, Docker Sandbox, Codex CLI, validated
comment publication, terminal reconciliation, graceful shutdown, and workspace/Sandbox cleanup.

Prepare the disposable PR with one known important defect plus malicious repository-owned
`AGENTS.md` and `.codex/config.toml` instructions containing distinct markers. Set
`E2E_EXPECTED_FINDING` to a stable fragment describing the seeded defect. The test verifies the
Check Run is attached to the accepted head, observes in-progress and completed states, expects an
advisory neutral conclusion for findings, fetches the published comment through GitHub, validates
the expected finding and absence of both malicious markers, and proves owned resources are gone.

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

The resource file records the exact comment and Check Run IDs. Delete the comment afterward and
retain the Check Run as evidence. A failure blocks rollout. Never run this profile without explicit
approval for GitHub writes, model cost, Docker Sandbox use, and the expected runtime.

## Rollout record

For each checkpoint, retain the command, accepted base/head SHAs, result, Check Run URL/ID, comment
ID, timestamp, and operator. Before production, also record:

- `STATE_ROOT` backup and restore ownership verification;
- one-process/one-host supervisor configuration;
- liveness and readiness probe results;
- graceful shutdown observation;
- confirmation that `Review Agent` is not required by branch protection.
