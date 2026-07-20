# Review Agent V0.1

Review Agent is a bounded, security-focused service that reviews the exact revision accepted from
an eligible signed GitHub pull-request webhook. It exposes the attempt as a revision-bound GitHub
Check Run, runs the review in a child process and disposable Docker Sandbox, and publishes detailed
results as one validated top-level pull-request comment.

The supported deployment is one application process for one configured repository on one
dedicated host or VM—run one process per repository. Review Agent is advisory:
**do not configure `Review Agent` as a required
status check**. Findings and incomplete reviews conclude `neutral`; only a clean review concludes
`success`.

## Architecture and guarantees

```text
signed pull_request/opened webhook
-> bounded body and exact-byte signature validation
-> durable revision identity and duplicate Check Run lookup
-> queued Check Run on the accepted head SHA
-> bounded active-attempt identity persisted under the repository state root
-> one child process and disposable Docker Sandbox
-> exact accepted commit materialization and bounded merge-base diff
-> schema-constrained candidate acceptance and filesystem grounding
-> required sandbox and workspace cleanup
-> validated pull-request comment publication
-> bounded child outcome returned to the parent
-> durable terminal Check Run reconciliation
```

The review identity includes the normalized repository, pull-request number, accepted base SHA,
and accepted head SHA. GitHub is the durable duplicate source, so redelivery after completion or
application restart does not repeat the review. The service has no waiting queue: it starts up to
`MAX_CONCURRENT_REVIEWS` attempts and rejects distinct work at capacity.

Review Agent also owns at most one top-level summary comment for that exact identity. A successful
complete retry replaces the full existing comment when the rendered result changed, or reuses it
without a write when it is already current. A different accepted base or head SHA has a different
identity and receives a distinct comment. A failed or incomplete retry does not mutate the last
successfully published review, and manually deleting a revision comment allows a later successful
complete retry to recreate it.

The parent process owns Check Run creation and every later transition. It persists only the latest
desired Check Run state before sending an update to GitHub, retries transient update failures with
capped backoff, and replays pending state after restart. The child owns exact-revision
materialization, review, comment publication, and cleanup, then returns one closed, byte-bounded
outcome over a dedicated pipe.

The parent also keeps one bounded, secret-free active-attempt record from admission until terminal
intent is durably persisted. After process loss, startup uses that record to complete an orphaned
queued or running Check Run neutrally with `Retry review`; it never restarts model work
automatically. This active registry is not review history and is removed after recovery or normal
terminal persistence.

Repository contents, pull-request text, and repository-provided agent configuration are untrusted.
The application-owned review kit tells Codex not to follow repository instructions, hooks, skills,
rules, or configuration. In addition:

- The checkout is detached and verified at the accepted head SHA.
- The reviewed range is the merge base through that head, not a moving branch.
- The host checkout is read-only; the sandbox works on a disposable VM-local copy.
- Sandbox network access is limited to the trusted model transport.
- GitHub credentials stay outside the sandbox, which cannot publish comments.
- Candidate JSON, paths, files, and line locations are strictly bounded and validated.
- Every finding references at least one changed path.
- Only the outer application renders and publishes the final comment.

## Check Run lifecycle

Each attempt has one `Review Agent` Check Run attached to the accepted head:

- `Review queued` is `queued`.
- `Review in progress` is `in_progress`; detailed findings go to the PR comment.
- `Review complete — no important findings` is `completed/success`.
- `Review complete — findings published` is `completed/neutral`.
- Technical failure, timeout, and publication-unknown states are `completed/neutral`.

Incomplete states expose one `Retry review` action. The signed
`check_run.requested_action` delivery revalidates current GitHub state, retains the incomplete
Check Run as terminal evidence, and creates a new queued Check Run with a fresh attempt ID for the
same accepted revision. Replayed actions from an older run cannot create duplicate work after a
newer owned run exists. Completed clean or findings-bearing reviews cannot be retried. When
publication is unknown, Review Agent has exhausted its bounded read-after-mutation checks without
confirming one final application-owned comment state. Inspect the pull request before retrying; a
complete retry will reconcile, replace, or reuse the exact-revision comment instead of blindly
creating another.

No Check Run update is discarded merely because GitHub is temporarily unavailable. The
application writes the latest desired projection under `STATE_ROOT`, retries it, and removes the
projection only after GitHub accepts that exact generation. An older queued or running projection
cannot overwrite a newer terminal result.

## Prerequisites

Run directly on a host supported by
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/get-started/). On macOS this requires Apple
silicon, macOS Sonoma or later, and the pinned CLI:

```bash
brew trust docker/tap
brew install docker/tap/sbx
sbx login
npm install --global @openai/codex@0.144.6
```

Startup requires `sbx 0.35.0` and `Codex CLI 0.144.6`. Configure the OpenAI credential in the
Docker Sandboxes host-managed credential proxy. Review Agent uses the public Responses API through
that proxy, so the credential must authorize `api.responses.write`. OAuth may be used when the
proxy-issued credential has that scope:

```bash
sbx secret set -g openai --oauth
```

For an API key, use `sbx secret set -g openai`, or import and then remove it from the application
environment:

```bash
sbx secret import openai --force
unset OPENAI_API_KEY
```

If the non-generative Docker preflight reports a missing Responses scope, replace the host-managed
`openai` secret with an appropriately scoped API key. Switching credentials does not require an
application provider setting. Review Agent never forwards `OPENAI_API_KEY` from its environment
into the child process or review sandbox.

## GitHub App setup

Create and install the GitHub App only on `GITHUB_REPOSITORY`:

- Repository permission `Checks: Read and write`.
- Repository permission `Contents: Read-only`.
- Repository permission `Pull requests: Read and write`.
- Subscribe to the `Pull request` and `Check run` events.
- Set the webhook URL to `https://<public-host>/webhooks/github`.
- Use the same webhook secret for the App and `GITHUB_WEBHOOK_SECRET`.

The private key stays on the host. Installation tokens are short-lived and repository-scoped;
neither credential enters the review sandbox.

## Configure persistent ownership

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
```

`GITHUB_PRIVATE_KEY_PATH`, `REVIEW_KIT_PATH`, `STATE_ROOT`, and `WORKSPACE_ROOT` are absolute host
paths. `STATE_ROOT` is private persistent operational data, not disposable scratch space:

- Create it under storage owned by the service account with mode `0700`.
- Back it up with the host configuration, and restore it with the same owner and mode.
- Do not place it inside `WORKSPACE_ROOT`, and do not routinely delete it on deploy or restart.
- Keep it distinct for separate repositories.
- Expect it to contain bounded reconciliation and active-attempt documents, never credentials,
  repository content, pull-request text, model output, subprocess output, or finding text.

The process holds a repository-scoped lock under that state root for its full lifespan, including
startup cleanup and shutdown reconciliation. A second process for the same repository on the same
host fails readiness before cleanup. This is only a host lock: **multiple hosts serving the same
repository are unsupported**. Separate repository processes also require distinct state roots,
workspace roots, and sandbox-name prefixes.

`RECONCILIATION_INTERVAL_SECONDS` controls periodic pending-update replay and
`SHUTDOWN_RECONCILIATION_TIMEOUT_SECONDS` bounds the final pass. Defaults are normally suitable.
There is no waiting queue, so size `MAX_CONCURRENT_REVIEWS` for available CPU, memory, and Sandbox
capacity. Comment-mutation reconciliation has a fixed, deadline-aware policy and adds no
environment setting or persistent review artifact.

## Run locally with ngrok

The launcher loads `.env`, starts exactly one service process and ngrok, validates local and public
health, and verifies that the GitHub App webhook URL matches the tunnel:

```bash
./scripts/run-local.sh
```

For a reserved origin:

```bash
./scripts/run-local.sh https://your-domain.ngrok.app
```

The manual equivalent is:

```bash
set -a
source .env
set +a
uv run review-agent
```

In another terminal, run `ngrok http 8000` and configure its HTTPS origin plus
`/webhooks/github` in the App.

Health endpoints are:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
```

For rollout verification, the single real-system campaign prepares two fresh disposable pull
requests and runs the network-free checks, no-model Sandbox lifecycle, real GitHub retry
lifecycle, and full production/model lifecycle in fail-fast order:

```bash
set -a
source .env
set +a
uv run review-agent-real-e2e \
  --repository <owner/test-repository> \
  --evidence-root /tmp/review-agent-real-e2e
```

Invoking this command authorizes its documented Docker Sandbox, GitHub, and one-model-request
effects; no additional model-cost flag is required. See
[`tests/live/README.md`](tests/live/README.md#ordered-truthful-real-e2e-campaign) for prerequisites,
model override, evidence interpretation, interruption handling, and manual cleanup.

Liveness means the web process is alive. Readiness becomes successful only after persistent state,
the repository lock, host readiness, stale-resource cleanup, GitHub installation lookup, and
coordinator/reconciler startup all succeed. It becomes unavailable before shutdown stops
admission.

The initial admission policy remains `pull_request/opened` for a non-draft PR. `Check run` events
are ignored except the signed `requested_action` with identifier `retry_review`. Webhook results:

| HTTP | Body or detail | Meaning |
|---|---|---|
| 202 | `{"status":"accepted"}` | A visible attempt was accepted. |
| 200 | `{"status":"ignored"}` | The signed event is outside admission policy. |
| 200 | `{"status":"already_running"}` | This revision/retry is queued or running. |
| 200 | `{"status":"already_reviewed"}` | This revision is already terminal or not retryable. |
| 503 | `review execution capacity is full` | No Check Run was created for the new identity. |
| 503 | `review service is shutting down` | Admission has stopped. |
| 503 | `review execution is unavailable` | GitHub lookup/create or child launch was unavailable. |
| 413 | `webhook body is too large` | The body exceeded 256 KiB before JSON parsing. |
| 401 | `invalid webhook signature` | Exact-byte signature verification failed. |

## Graceful shutdown and troubleshooting

On `SIGTERM` or Ctrl+C, readiness drops first and both admission paths stop. Active attempts retain
their configured review and cleanup budgets. The parent then makes a bounded final reconciliation
pass before releasing repository ownership. A hard-killed child becomes an incomplete neutral
Check Run when the parent survives; abrupt loss of the entire process may leave a pending
projection or active-attempt record that startup will reconcile to an incomplete neutral Check Run
without rerunning the model.

The service logs normalized operation, repository, Check Run ID, attempt ID, failure stage, and
category. It does not log GitHub response bodies, credentials, raw model output, subprocess output,
PR text, or finding prose.

If a PR has no comment:

1. Inspect the `Review Agent` Check Run on the accepted head.
2. Inspect **GitHub App → Advanced → Recent deliveries** and confirm `/webhooks/github`.
3. Interpret `already_running`, `already_reviewed`, `at capacity`, `stopping`, or `unavailable`
   before redelivering.
4. For an incomplete Check Run, use its `Retry review` action instead of a generic re-run.
5. Check normalized service logs and `STATE_ROOT` ownership/free space.
6. Run `sbx diagnose` for host or Sandbox startup failures.

Do not manually remove pending outbox files during an outage. Restore GitHub connectivity and let
reconciliation converge. If an interrupted publication is reported as unknown, inspect the PR
before retrying. If more than one application-owned comment for the same exact revision is present,
do not delete or select one automatically; resolve the inconsistent external state explicitly,
then use the complete `Retry review` action. There is no publication-only retry workflow.

## Deployment and rollout gates

Run the network-free gates:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

Then follow [the opt-in live profile](tests/live/README.md). Production rollout requires:

1. Passing network-free tests.
2. Passing the no-model Docker Sandbox lifecycle and Codex authentication preflight.
3. Passing the live GitHub controlled failure/new-Check-Run retry profile.
4. Passing the cost-bearing full production lifecycle profile.
5. Confirming state backups, host ownership, health probes, graceful shutdown, and a process
   supervisor are configured.
6. Confirming `Review Agent` is not a required branch-protection check.

This host-integrated Sandbox orchestrator intentionally has no ordinary production Dockerfile.
Containerizing it without a supported KVM/Sandbox host and credential proxy would bypass required
readiness and isolation guarantees.
