<p align="center">
  <img src="./logo.png" alt="SpeCodeReview" width="760">
</p>

# SpeCodeReview v0.1.0

[![CI](https://github.com/ivand200/specode_review/actions/workflows/ci.yml/badge.svg)](https://github.com/ivand200/specode_review/actions/workflows/ci.yml)

SpeCodeReview is a security-focused GitHub App that reviews the exact commit accepted from a
signed pull-request webhook. It runs Codex in a disposable Docker Sandbox, validates and grounds
the result, then publishes one revision-bound pull-request comment.

This is a production-oriented prototype with a deliberately narrow deployment model: one process,
one GitHub App, and one dedicated host or VM. It serves every repository authorized for that App.
The product is **SpeCodeReview**; `review-agent` remains the transitional Python package and CLI
until the planned clean package-identity cutover.

## Why it exists

Automated review becomes risky when a moving branch, untrusted repository instructions, leaked
credentials, or a half-completed GitHub write can change what was reviewed or reported.
SpeCodeReview makes those failure modes explicit and bounded:

- Every attempt is bound to an immutable repository, pull request, base SHA, and head SHA.
- Repository code, PR text, and repository-provided agent configuration are treated as untrusted.
- Codex runs in a disposable microVM with restricted network access.
- GitHub credentials stay on the host; only the outer application can publish.
- Candidate JSON, file paths, changed locations, process output, and runtime are validated and
  bounded.
- Cleanup must complete before the final comment can be published.

## How it works

```mermaid
flowchart LR
    GH[GitHub<br>signed webhook] --> A[Admission<br>exact revision identity]
    A --> P[In-process lifecycle<br>bounded capacity]
    P --> R[Synchronous review transaction]
    R --> S[Disposable Docker Sandbox<br>Codex + fixed diff]
    S --> V[Schema validation<br>filesystem grounding]
    V --> GH
```

The host materializes and verifies the accepted head commit, computes a bounded merge-base diff,
and gives the sandbox a disposable copy. The model returns a schema-constrained candidate; the
application verifies that every finding refers to changed repository content before rendering the
final comment.

The exact-revision application-owned comment is the duplicate source. A redelivery for an active
or completed identity does not repeat work. There is intentionally no waiting queue: the service
starts up to
`MAX_CONCURRENT_REVIEWS` attempts and rejects distinct work at capacity.

## Key engineering decisions

| Decision | Failure mode addressed |
|---|---|
| Bind work to accepted base and head SHAs | Reviewing a moving branch or reporting against the wrong revision |
| Keep GitHub credentials outside the sandbox | Untrusted code or model tools publishing directly |
| Own one exact-revision comment | Duplicate comments across delivery and retry |
| Cleanup before publication | A developer seeing a result from an incompletely isolated transaction |
| Use one process and host-wide capacity limit | Queues and hidden per-repository reservations |

## Quick start

### Prerequisites

- Python 3.12 or later and [`uv`](https://docs.astral.sh/uv/)
- Git, curl, Node.js/npm, and [ngrok](https://ngrok.com/) for local webhook delivery
- A host supported by
  [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/get-started/)
- `sbx 0.35.0` and `Codex CLI 0.144.6` (enforced at startup)

On supported macOS hosts:

```bash
brew trust docker/tap
brew install docker/tap/sbx
sbx login
npm install --global @openai/codex@0.144.6
```

Store the OpenAI credential in the Docker Sandboxes host-managed credential proxy. For OAuth:

```bash
sbx secret set -g openai --oauth
```

For an API key, use `sbx secret set -g openai` or import `OPENAI_API_KEY` with
`sbx secret import openai --force`, then remove it from the application environment. The
credential proxy supplies it to trusted model transport without exposing the real value inside
the sandbox.

### Configure the GitHub App

Install the GitHub App on every repository the service should review:

- **Contents:** read-only
- **Pull requests:** read and write
- Event: **Pull request**
- Webhook URL: `https://<public-host>/webhooks/github`

Use the same webhook secret for the App and `GITHUB_WEBHOOK_SECRET`. Keep the App private key on
the host.

### Configure and run

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
```

Edit `.env` with the App identity and secret, complete stable HTTPS webhook URL, model policy, and
optional ingress values. Production paths and runtime limits are fixed by the application and
documented in `.env.example`.

`MAX_CONCURRENT_REVIEWS` defaults to `3` and accepts only `1` through `5`. Size it from measured
host behavior.

Load the environment and start the loopback service:

```bash
set -a
source .env
set +a
uv run review-agent
```

Health probes are available at `/health/live` and `/health/ready`.

## Review lifecycle

The service admits non-draft `opened`, `synchronize`, `ready_for_review`, and `reopened` pull
requests unless they carry the `no-review` label. Preflight proves App access and exact-revision
idempotency before capacity is consumed. Accepted work runs one cleanup-before-publication
transaction. A clean review and a review with findings both publish one top-level comment;
technical failures are visible only in safe structured operator logs.

## Verification

The default suite is network-free:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

The real-system campaign adds a no-model Sandbox lifecycle, a controlled GitHub retry lifecycle,
and one full production/model lifecycle:

```bash
set -a
source .env
set +a
uv run review-agent-real-e2e \
  --repository <owner/test-repository> \
  --evidence-root /tmp/review-agent-real-e2e
```

This command creates documented external resources and makes one model request. Read the
[live rollout guide](tests/live/README.md#ordered-truthful-real-e2e-campaign) before running it;
the guide covers prerequisites, evidence, interruption handling, and cleanup.

## Operational constraints

- Run exactly one application process on one host. Multiple hosts serving the same repository are
  unsupported.
- There is no durable workflow state, retry queue, or per-repository capacity reservation.
- On shutdown, readiness and admission close before already accepted bounded reviews are drained.
- The project intentionally has no production Dockerfile: the orchestrator must run on a supported
  Docker Sandboxes host with its microVM and credential-proxy guarantees.

## Project status and license

SpeCodeReview is a v0.1.0 production-oriented prototype intended to demonstrate exact-revision
review, explicit trust boundaries, and failure-aware GitHub integration. No license file is
currently provided.
