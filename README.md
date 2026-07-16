# Review Agent V0.1

A bounded, single-worker service that reviews the exact revision from an eligible signed
GitHub pull-request webhook inside a disposable Docker Sandbox and publishes one validated
top-level comment.

## Production startup

Install the locked project and copy `.env.example` to a protected environment file. Run exactly
one process and one web-server worker:

```bash
uv sync --locked
set -a
source .env
set +a
uv run review-agent
```

Startup fails before the socket accepts traffic unless all settings and secret paths are valid,
the dedicated workspace root is writable, Git is available, Docker Sandboxes diagnostics pass,
the application-owned kit validates, and the runtime versions exactly match `sbx 0.35.0` and
`Codex CLI 0.144.5`. Startup errors contain a normalized stage only; subprocess output and secret
values are not logged.

The Docker Sandboxes host must support microVM sandboxes, be signed in, and have an OpenAI
credential configured in its host-managed credential proxy. Prefer OAuth with
`sbx secret set -g openai --oauth`; a proxy-stored API key configured with
`sbx secret set -g openai` or `sbx secret import openai --force` is also supported. Do not leave
`OPENAI_API_KEY` in the application environment after importing it. The proxy keeps the raw
credential on the host and gives the sandbox only a placeholder. The GitHub private key stays on
the host, GitHub installation tokens are ephemeral, and the sandbox receives neither credential.
The service does not use `pydantic-ai` and does not claim model-request, tool-call, or token limits
that Codex CLI cannot enforce.

The queue is in memory and has capacity ten. V0.1 has no delivery deduplication, retries, or crash
recovery. An abrupt restart can lose queued or active work; redeliver the webhook manually.

## Verification profiles

The normal, network-free feedback loop is:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

Normal tests use fake GitHub and runner adapters and require no GitHub, Docker, OpenAI credentials,
network access, or model budget. Docker lifecycle and live profiles are opt-in and documented in
[`tests/live/README.md`](tests/live/README.md). Run the full checkpoint C before rollout; a failure
blocks rollout rather than weakening validation or isolation.
