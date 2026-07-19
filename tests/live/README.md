# Operational rollout profiles

All live profiles are opt-in and must target a disposable, open, non-draft pull request in a
dedicated repository whose name contains `test`. Normal `pytest` runs skip them. Use a fresh
accepted base/head revision: durable Check Run identity intentionally makes a repeated completed
run return `already_reviewed`. Before either profile starts its service or creates any external
resource, it derives that deterministic review identity and requires both:

- zero application-owned Check Runs for the accepted revision; and
- zero exact-marker review comments performed by the configured numeric GitHub App ID.

If either exists, stop and manually prepare a new accepted SHA on the disposable PR. The profiles
do not create commits, move branches, delete old evidence, or repair a polluted fixture.

## Checkpoint B: real GitHub lifecycle without a model

This profile starts the HTTP service on a real socket and sends signed webhooks through the public
interface. A controlled child first fails, allowing the test to verify a real neutral Check Run
with the exact `Review incomplete — technical failure` title. The retry button remains part of the
write presentation; the subsequent read is intentionally actionless, matching GitHub's response
contract. The profile then sends a real-shaped signed `check_run.requested_action`, verifies queued
and running states, and a fresh attempt ID on the same Check Run. The successful controlled retry
requires the exact `Review complete — no important findings` title, then exercises the public
publication interface against GitHub:

1. create the exact-revision clean comment;
2. replace that complete comment with a findings result while retaining its comment ID;
3. reuse the already-current findings comment without another write;
4. delete the comment through GitHub as an external/manual action; and
5. recreate one clean exact-revision comment with a new comment ID before verifying
   `completed/success`.

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

Use a manually prepared fresh accepted SHA for this invocation. The preflight runs before the HTTP
service and controlled launcher exist, so polluted evidence fails without a Check Run/comment
write or resource-record append.

The resource file records the Check Run ID, final comment ID, PR number, and cleanup instruction.
Delete the recorded automated review comment manually. GitHub Check Runs cannot be deleted through
this profile; retain it as rollout evidence. After interruption, inspect both the PR and its checks
because GitHub writes can succeed before the local cleanup record is appended.

The live harness cannot safely and deterministically make GitHub accept a comment mutation while
hiding only its response. Ambiguous create/update reconciliation is therefore covered by the
controlled, network-free publication tests, which inject a lost mutation result and verify bounded
read recovery without issuing a second mutation. This limitation must remain in the rollout record
rather than being represented as live evidence.

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
Check Run is attached to the accepted head, observes in-progress and completed states, and then
requires exactly one application-owned Check Run with the exact
`Review complete — findings published` title and advisory neutral conclusion. Through the same
typed publication policy used by production, it also requires exactly one comment with the
complete deterministic revision marker and the configured numeric App ID. That confirmed comment
must contain the expected finding and neither malicious marker.

The cleanup record is post-success evidence, not an attempt log. It is appended only after the
production server shuts down gracefully, the exact workspace and Sandbox cleanup checks pass, and
all Check Run/comment provenance and content assertions succeed. It contains only the verified
Check Run and comment IDs plus bounded cleanup guidance; it never contains the finding or comment
body. Any clean, timeout, publication-unknown, technical-failure, missing, duplicate, foreign-App,
wrong-marker, or hostile-text result fails without appending a record.
Prepare a manually fresh accepted SHA immediately before the run. Its freshness preflight occurs
before installation-token reuse, production service assembly, Docker Sandbox creation, or model
cost.

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

- initial creation, same-revision replacement, already-current reuse, and deletion/recreation
  dispositions from Checkpoint B;
- that ambiguous-publication reconciliation passed in the network-free suite but was not induced
  against live GitHub;
- no GitHub App permission beyond existing Checks write, Contents read, and Pull requests write;
- no comment-reconciliation environment setting, persistent review artifact, publication-only
  retry action, or retained model output;
- `STATE_ROOT` backup and restore ownership verification;
- one-process/one-host supervisor configuration;
- liveness and readiness probe results;
- graceful shutdown observation;
- confirmation that `Review Agent` is not required by branch protection.
