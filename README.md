# Review Agent V0.1

A bounded, security-focused service that reviews the exact revision accepted from an eligible
signed GitHub pull-request webhook. Each accepted review runs in its own child process and
disposable Docker Sandbox, then publishes one validated top-level GitHub comment.

V0.1 is intended for a single dedicated host and one configured GitHub repository. It favors a
small, high-signal review contract: report only blocking and important defects, or state that none
were found.

An example comment has this shape:

```text
# Automated code review
Reviewed commit range: <merge-base>..<accepted-head>

## Issues found
### Finding 1: Existing data can be overwritten
- Severity: important
- Locations:
  - feature.py:42
- Evidence: ...
- Impact: ...
- Suggested fix: ...
```

## Architecture

```text
signed GitHub pull_request/opened webhook
-> signature, repository, action, and draft validation
-> in-memory admission and active-attempt deduplication
-> one child process for the complete review attempt
-> exact accepted commit materialization and bounded merge-base diff
-> disposable Docker Sandbox with the application-owned review kit
-> Codex schema-constrained candidate output
-> outer-process validation and filesystem grounding
-> one top-level GitHub review comment
-> sandbox and workspace cleanup
```

The webhook process does not maintain a waiting queue. It starts up to
`MAX_CONCURRENT_REVIEWS` child attempts, defaulting to one and bounded between one and ten.
Distinct requests received at capacity are rejected immediately. The admission record and
duplicate detection are process-local and exist only while an attempt is active.

Each child owns one complete attempt: GitHub authentication, repository materialization, review,
publication, and cleanup. The parent monitors the child's process group and applies a hard
deadline. This is per-review process isolation on one host; it is not yet an external job runner
or durable queue.

## Trust model and guarantees

Repository contents, pull-request text, and repository-provided agent configuration are untrusted
data. The application-owned review kit instructs Codex not to follow repository instructions,
hooks, skills, rules, or configuration.

The important guarantees are:

- The accepted base and head commits are materialized explicitly.
- The checkout is detached at the accepted head SHA and verified before review.
- The review range is the merge base through that accepted head, not a moving branch.
- The host checkout is mounted read-only; the sandbox works on a disposable VM-local copy.
- Sandbox network access is limited by the trusted review kit to model transport.
- The GitHub App private key and installation token remain outside the review sandbox.
- The sandbox cannot publish comments.
- Codex output must satisfy a closed JSON Schema and a byte limit.
- The outer process validates paths, files, and line locations against the fixed checkout.
- Every accepted finding must reference at least one changed path.
- Only the outer application renders and publishes the final GitHub comment.

## Scope and non-goals

V0.1 deliberately supports:

- GitHub only, through a GitHub App.
- One repository configured by `GITHUB_REPOSITORY`.
- The original `opened` event for a non-draft pull request.
- One validated top-level comment per successful attempt.
- At most 100 changed files and 5,000 changed text lines.

V0.1 does not provide a durable attempt store, waiting queue, delivery-level idempotency, automatic
retry, crash recovery, or publication retry. Active-attempt duplicate detection prevents only an
identical repository, pull request, base SHA, and head SHA from running concurrently in the current
application process. After the attempt finishes—or after a restart—the same delivery can run
again and can publish another comment.

## Prerequisites

The service must run directly on a host that supports
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/get-started/). On macOS this means Apple
silicon, macOS Sonoma or later, and the pinned `sbx` CLI:

```bash
brew trust docker/tap
brew install docker/tap/sbx
sbx login
```

Startup requires these exact runtime versions:

```bash
npm install --global @openai/codex@0.144.5
sbx version
codex --version
```

The reported versions must be `sbx 0.35.0` and `codex-cli 0.144.5`.

Configure the OpenAI credential in the Docker Sandboxes host-managed credential proxy. OAuth is
preferred:

```bash
sbx secret set -g openai --oauth
```

A proxy-stored API key is also supported:

```bash
sbx secret set -g openai
# Or import OPENAI_API_KEY once:
sbx secret import openai --force
unset OPENAI_API_KEY
```

Do not leave `OPENAI_API_KEY` in the application environment after importing it. The proxy keeps
the raw credential on the host and gives the sandbox only a placeholder.

## GitHub App setup

Create and install a GitHub App only on the repository named by `GITHUB_REPOSITORY`. Configure:

- Repository permission `Contents: Read-only`.
- Repository permission `Pull requests: Read and write`.
- Subscribe to the `Pull request` event.
- Set the webhook URL to `https://<public-host>/webhooks/github`.
- Set the same webhook secret in the GitHub App and `GITHUB_WEBHOOK_SECRET`.

The application private key stays on the host. Installation tokens are short-lived, and neither
credential is passed into the review sandbox.

## Configure the service

Install the locked project and create a protected environment file:

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
```

Edit `.env`. `GITHUB_PRIVATE_KEY_PATH`, `REVIEW_KIT_PATH`, and `WORKSPACE_ROOT` must be absolute
host paths. `GITHUB_WEBHOOK_SECRET` is the literal GitHub App webhook secret, not a path. Leave
`NGROK_URL` empty on an ngrok free plan, or set it to a reserved HTTPS origin that belongs to your
ngrok account. `MAX_CONCURRENT_REVIEWS` is optional, defaults to `1`, and accepts values from `1`
through `10`. There is no waiting queue, so size this for the host's available CPU, memory, and
Docker Sandbox capacity.

## Run locally with ngrok

The Uvicorn listener at `0.0.0.0:8000` is not reachable from GitHub by itself. Run the service and
ngrok in separate terminals, and keep both processes alive. Run exactly one process for the
webhook service. That process owns in-memory admission and active-attempt identity; accepted
reviews run in separate child processes.

The recommended one-command launcher loads `.env`, starts both processes, discovers the assigned
ngrok URL, verifies the local and public endpoints, reads the GitHub App's webhook configuration,
and refuses to report readiness unless the configured webhook URL exactly matches the running
tunnel. If the URLs differ, it keeps the current tunnel alive and waits for you to update the
GitHub App instead of restarting ngrok and changing the URL again. It stops both processes on
Ctrl+C:

```bash
./scripts/run-local.sh
```

If your ngrok account owns a reserved endpoint, you can configure it in `.env` or override it for
one run:

```bash
./scripts/run-local.sh https://your-domain.ngrok.app
```

On the free plan, ngrok reuses the account's automatically assigned dev domain. The launcher
discovers and prints it after startup. Copy the displayed `https://.../webhooks/github` URL into
the GitHub App's webhook configuration before opening or redelivering a pull request. Because it
is the account's assigned dev domain, it can be reused after restarting ngrok.

For manual startup, use the two-terminal equivalent below.

Terminal 1:

```bash
set -a
source .env
set +a
uv run review-agent
```

Terminal 2:

```bash
ngrok http 8000
```

Use the forwarding URL printed by ngrok and set the GitHub App webhook URL to:

```text
https://<your-ngrok-domain>/webhooks/github
```

Start both processes before opening the pull request. A healthy local and public path can be
checked without sending a review:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/openapi.json >/dev/null
curl --fail --silent --show-error https://<your-ngrok-domain>/openapi.json >/dev/null
```

The second command must return success. `ERR_NGROK_3200` means the configured ngrok endpoint is
offline.

The service accepts only a non-draft pull request's original `opened` event. If that delivery
failed, open the GitHub App settings, go to **Advanced → Recent deliveries**, select the failed
`pull_request / opened` delivery, and choose **Redeliver**. Pushing another commit or reopening
the pull request does not trigger V0.1.

An eligible webhook returns HTTP `202` with `{"status":"accepted"}`. A valid but ineligible event
returns HTTP `200` with `{"status":"ignored"}`. An identical attempt that is already active returns
HTTP `200` with `{"status":"already_running"}`. A request received at capacity, during shutdown,
or when a child cannot be launched returns HTTP `503`; it is not retained for later execution.
Signature failures return HTTP `401`.

## Logs and troubleshooting

The service logs to stdout/stderr only; it does not create a log file. Keep the terminal or use a
process supervisor that captures its output. A review failure is logged with the repository, pull
request number, accepted head SHA, stage, and normalized category without subprocess output or
secret values.

If a pull request produces no comment:

1. Check the GitHub App's **Advanced → Recent deliveries** page.
2. Confirm the delivery URL ends in `/webhooks/github`.
3. Confirm the delivery returned `202`; a `503` means the attempt was not retained, while a fast
   ngrok `404` usually means the tunnel is offline.
4. Check the service terminal for correlated `review process` and `review attempt failed` records
   after an accepted delivery.
5. Run `sbx diagnose` if startup or sandbox creation fails.

Startup fails before the socket accepts traffic unless all settings and secret paths are valid,
the dedicated workspace root is writable, Git is available, Docker Sandboxes diagnostics pass,
the application-owned kit validates, and the runtime versions exactly match `sbx 0.35.0` and
`Codex CLI 0.144.5`. Startup errors contain a normalized stage only; subprocess output and secret
values are not logged.

The service does not use `pydantic-ai` and does not claim model-request, tool-call, or token limits
that Codex CLI cannot enforce.

During graceful shutdown the service stops admitting work immediately and waits for every active
child attempt. A child has `REVIEW_TIMEOUT_SECONDS` for normal attempt work, including publication
and normal cleanup. The parent allows the configured sandbox cleanup timeout beyond that deadline,
then sends `SIGTERM` to the child's process group. If the group remains alive for another cleanup
timeout, the parent sends `SIGKILL` and performs a bounded parent-side cleanup attempt.

An abrupt host or application-process loss can still lose active work without a final status.
V0.1 has no durable delivery deduplication, automatic retries, or crash recovery. Use the GitHub
App's **Advanced → Recent deliveries** page to redeliver the affected `pull_request / opened`
webhook manually. Because completed attempts are not recorded durably, confirm whether a comment
was already published before redelivery.

## Why there is no production Dockerfile

This process is an orchestrator for Docker Sandbox microVMs, not a normal stateless web
application. On macOS, `sbx` is a host-integrated macOS executable; it cannot run inside a Linux
application container. Mounting `/var/run/docker.sock` would expose the host Docker daemon but
would not provide the required Docker Sandboxes microVM host, credential proxy, or host workspace
semantics.

For that reason, an ordinary Dockerfile would build successfully but fail the mandatory
`sbx diagnose` startup check on this development setup. Run V0.1 directly on a compatible,
dedicated host or VM. A future container deployment should target a supported Linux runner with
KVM and explicitly preserve the current isolation and credential model; it should not bypass
readiness checks.

## Verification profiles

The normal, network-free feedback loop is:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

Normal tests use fake GitHub and raw-byte candidate adapters through real candidate acceptance.
They also exercise per-review child-process admission, capacity, duplicate detection, deadlines,
process-group termination, and cleanup without using a model. They require no GitHub, Docker,
OpenAI credentials, network access, or model budget. Docker lifecycle and live profiles are
opt-in and documented in
[`tests/live/README.md`](tests/live/README.md). Run the full checkpoint C before rollout; a failure
blocks rollout rather than weakening validation or isolation.
