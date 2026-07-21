# Production deployment

This document describes the deliberately narrow supported deployment: one SpeCodeReview process,
one GitHub App, and one dedicated Ubuntu host or VM. It serves every repository authorized for
that App.

## Prerequisites

Install `uv`, Git, curl, `runuser`, `systemd`, `sbx 0.35.0`, and Codex CLI `0.144.6` in the system
`PATH`. Managed reserved-ngrok ingress additionally requires ngrok `3.39.1`.

The host must support Docker Sandboxes. GitHub credentials stay on the host, while model
credentials belong in the Docker Sandboxes host-managed credential store and must not be placed in
the application environment.

## Prepare the release

Clone the repository at `/opt/specode-review`, fetch release tags, and detach at the exact supported
tag:

```bash
sudo git clone <repository-url> /opt/specode-review
sudo git -C /opt/specode-review fetch --tags
sudo git -C /opt/specode-review checkout --detach v0.1.0
```

Create `/opt/specode-review/.env` from `.env.example`. Configure the GitHub App identity, webhook
secret, complete stable HTTPS webhook URL, model policy, and optional ingress values. Place the
unencrypted RSA GitHub App key at `/opt/specode-review/.secrets/github-app.pem`.

Configure the model credential as the same OS identity that runs the service:

```bash
sudo -u specode-review env -i \
  HOME=/var/lib/specode-review \
  PATH=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin \
  sbx secret set -g openai --oauth
```

If the service identity does not exist yet, run the installer once to provision the managed host
state, configure the credential, and rerun the same installer command.

For headless Docker authentication, use a Docker Personal Access Token with at least Read scope:

```bash
printf '%s' "$DOCKER_PAT" | \
  sudo -u specode-review sbx login --username <your-docker-id> --password-stdin
```

## Install or repair

```bash
cd /opt/specode-review
sudo ./scripts/install.sh --release v0.1.0
```

The installer rejects branches, commits that are not exactly tagged, unsupported tags, malformed
or placeholder configuration, model credentials in the application environment, unsafe secret
files, and unpinned host tools. It then:

- converges the non-login service user and restrictive managed paths;
- installs the locked non-development environment with `uv`;
- runs a disposable no-model Sandbox capability probe;
- writes, enables, and starts `specode-review.service`.

When `NGROK_URL` and `NGROK_AUTHTOKEN` are set, the reserved HTTPS origin must match
`PUBLIC_WEBHOOK_URL`. The installer also writes and starts `specode-review-ngrok.service`.

After startup, installation waits up to ten minutes for the GitHub App webhook URL to match
`PUBLIC_WEBHOOK_URL`. A mismatch leaves the supervised units running and prints manual correction
instructions; the installer never changes GitHub App configuration.

## Operate the service

The service listens on `127.0.0.1:8000`, logs to `journald`, restarts after failures no more than
five times in five minutes, and allows up to twenty minutes for graceful shutdown.

```bash
sudo systemctl status specode-review
sudo journalctl -u specode-review --since today
sudo systemctl restart specode-review

# Managed ngrok mode
sudo systemctl status specode-review-ngrok
sudo journalctl -u specode-review-ngrok --since today
```

Health probes are exposed at `/health/live` and `/health/ready`.

Upgrade and rollback use the same installer operation after checking out another exact supported
release tag. The installer never runs `git clean`, deletes foreign Docker Sandboxes, or changes
GitHub App configuration.

## Verify an installation

Run the verifier after installation, upgrade, rollback, or repair while the service is otherwise
idle:

```bash
sudo ./scripts/verify-install.sh
```

It checks required units, local and public health, the configured GitHub App identity and webhook
URL, pinned host tools, and the trusted review kit. It then creates, limits, mounts, executes,
inspects, lists, and force-removes one network-denied temporary Sandbox and confirms that no
SpeCodeReview-owned Sandbox or workspace remains.

The verifier emits only bounded pass/fail evidence. It never invokes Codex, spends model tokens,
or publishes a GitHub review. Running it while a review is active can make that bounded resource
look stale.

## Operational constraints

- Run exactly one application process on one host. Multiple hosts serving the same repository are
  unsupported.
- There is no durable workflow state, retry queue, or per-repository capacity reservation.
- On shutdown, readiness and admission close before already accepted reviews are drained.
- The orchestrator has no production Dockerfile because it must run on a supported Docker
  Sandboxes host with its microVM and credential-proxy guarantees.
