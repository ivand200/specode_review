#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
LOCAL_HEALTH_URL="http://127.0.0.1:8000/health/ready"
SERVICE_PID=""
NGROK_PID=""
NGROK_PUBLIC_URL=""

usage() {
    printf 'Usage: %s [https://your-domain.ngrok.app]\n' "$0" >&2
    printf 'The URL argument overrides NGROK_URL from .env.\n' >&2
}

# shellcheck disable=SC2329  # Invoked by the EXIT trap.
cleanup() {
    local status=$?
    trap - EXIT INT TERM

    if [[ -n "${NGROK_PID}" ]] && kill -0 "${NGROK_PID}" 2>/dev/null; then
        kill "${NGROK_PID}" 2>/dev/null || true
    fi
    if [[ -n "${SERVICE_PID}" ]] && kill -0 "${SERVICE_PID}" 2>/dev/null; then
        kill "${SERVICE_PID}" 2>/dev/null || true
    fi

    [[ -z "${NGROK_PID}" ]] || wait "${NGROK_PID}" 2>/dev/null || true
    [[ -z "${SERVICE_PID}" ]] || wait "${SERVICE_PID}" 2>/dev/null || true
    exit "${status}"
}

exit_for_child() {
    local pid=$1
    local status

    set +e
    wait "${pid}"
    status=$?
    set -e
    if ((status == 0)); then
        status=1
    fi
    exit "${status}"
}

wait_for_url() {
    local url=$1
    local pid=$2
    local attempts=$3
    local label=$4
    local attempt=0

    while ((attempt < attempts)); do
        if curl --fail --silent --show-error --max-time 3 "${url}" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "${pid}" 2>/dev/null; then
            printf '%s stopped before becoming ready.\n' "${label}" >&2
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    printf 'Timed out waiting for %s at %s.\n' "${label}" "${url}" >&2
    return 1
}

wait_for_ngrok_url() {
    local pid=$1
    local attempts=$2
    local attempt=0
    local response
    local public_url

    while ((attempt < attempts)); do
        response="$(
            curl --fail --silent --show-error --max-time 3 \
                "http://127.0.0.1:4040/api/tunnels" 2>/dev/null || true
        )"
        if [[ -n "${response}" ]]; then
            public_url="$(
                printf '%s' "${response}" |
                    "${ROOT_DIR}/.venv/bin/python" -c \
                        'import json, sys
data = json.load(sys.stdin)
print(next(
    (
        tunnel["public_url"]
        for tunnel in data.get("tunnels", [])
        if tunnel.get("public_url", "").startswith("https://")
    ),
    "",
))' 2>/dev/null || true
            )"
            if [[ -n "${public_url}" ]]; then
                NGROK_PUBLIC_URL="${public_url%/}"
                return 0
            fi
        fi
        if ! kill -0 "${pid}" 2>/dev/null; then
            printf 'ngrok stopped before publishing a tunnel URL.\n' >&2
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    printf 'Timed out waiting for ngrok to publish a tunnel URL.\n' >&2
    return 1
}

github_webhook_url() {
    "${ROOT_DIR}/.venv/bin/python" -c '
import os
from pathlib import Path

from review_agent.github import GitHubAppClient

github = GitHubAppClient(
    repository="unused/unused",
    app_id=int(os.environ["GITHUB_APP_ID"]),
    private_key_path=Path(os.environ["GITHUB_PRIVATE_KEY_PATH"]),
)
try:
    print(github.webhook_url())
finally:
    github.close()
'
}

wait_for_github_webhook_url() {
    local expected_url=$1
    local configured_url=""
    local last_reported_url=""

    while kill -0 "${SERVICE_PID}" 2>/dev/null && kill -0 "${NGROK_PID}" 2>/dev/null; do
        if ! configured_url="$(github_webhook_url)"; then
            printf 'Unable to read the GitHub App webhook configuration.\n' >&2
            return 1
        fi
        if [[ "${configured_url}" == "${expected_url}" ]]; then
            return 0
        fi
        if [[ "${configured_url}" != "${last_reported_url}" ]]; then
            printf 'GitHub App webhook URL mismatch.\n' >&2
            printf 'Configured: %s\n' "${configured_url}" >&2
            printf 'Expected:   %s\n' "${expected_url}" >&2
            printf 'Update the GitHub App webhook URL. Waiting for it to match; press Ctrl+C to stop.\n' >&2
            last_reported_url="${configured_url}"
        fi
        sleep 5
    done

    printf 'A local review process stopped while waiting for the GitHub App webhook URL.\n' >&2
    return 1
}

if (($# > 1)); then
    usage
    exit 2
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    printf 'Environment file not found: %s\n' "${ENV_FILE}" >&2
    printf 'Create it with: cp .env.example .env\n' >&2
    exit 2
fi

for command_name in uv ngrok curl; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "${command_name}" >&2
        exit 2
    fi
done
if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    printf 'Project environment not found. Run: uv sync --locked\n' >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

REQUESTED_NGROK_URL="${1:-${NGROK_URL:-}}"
REQUESTED_NGROK_URL="${REQUESTED_NGROK_URL%/}"
if [[ -n "${REQUESTED_NGROK_URL}" ]]; then
    if [[ "${REQUESTED_NGROK_URL}" != https://* ]]; then
        printf 'NGROK_URL must be a complete HTTPS URL: %s\n' "${REQUESTED_NGROK_URL}" >&2
        exit 2
    fi
    NGROK_AUTHORITY="${REQUESTED_NGROK_URL#https://}"
    if [[ -z "${NGROK_AUTHORITY}" || "${NGROK_AUTHORITY}" == */* ||
        "${NGROK_AUTHORITY}" == *\?* || "${NGROK_AUTHORITY}" == *\#* ]]; then
        printf 'NGROK_URL must be an HTTPS origin without a path, query, or fragment.\n' >&2
        exit 2
    fi
fi

cd "${ROOT_DIR}"

if curl --fail --silent --max-time 2 "${LOCAL_HEALTH_URL}" >/dev/null 2>&1; then
    printf 'Port 8000 already has a running review service. Stop it first.\n' >&2
    exit 1
fi

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Starting review service...\n'
uv run review-agent &
SERVICE_PID=$!

if ! wait_for_url "${LOCAL_HEALTH_URL}" "${SERVICE_PID}" 60 "review service"; then
    exit 1
fi

if [[ -n "${REQUESTED_NGROK_URL}" ]]; then
    printf 'Starting ngrok at %s...\n' "${REQUESTED_NGROK_URL}"
    ngrok http 8000 \
        --url "${REQUESTED_NGROK_URL}" \
        --log stdout \
        --log-format logfmt &
else
    printf 'Starting ngrok with a free-plan assigned URL...\n'
    ngrok http 8000 \
        --log stdout \
        --log-format logfmt &
fi
NGROK_PID=$!

if ! wait_for_ngrok_url "${NGROK_PID}" 30; then
    exit 1
fi

if ! wait_for_url "${NGROK_PUBLIC_URL}/health/ready" "${NGROK_PID}" 30 "ngrok tunnel"; then
    exit 1
fi

EXPECTED_WEBHOOK_URL="${NGROK_PUBLIC_URL}/webhooks/github"
printf 'Verifying GitHub App webhook URL...\n'
if ! wait_for_github_webhook_url "${EXPECTED_WEBHOOK_URL}"; then
    exit 1
fi

printf '\nReview Agent is ready.\n'
printf 'Webhook URL: %s\n' "${EXPECTED_WEBHOOK_URL}"
if [[ -z "${REQUESTED_NGROK_URL}" ]]; then
    printf 'Ensure the GitHub App webhook URL matches the free dev-domain URL shown above.\n'
fi
printf 'Press Ctrl+C to stop the service and ngrok.\n\n'

while kill -0 "${SERVICE_PID}" 2>/dev/null && kill -0 "${NGROK_PID}" 2>/dev/null; do
    sleep 1
done

if ! kill -0 "${SERVICE_PID}" 2>/dev/null; then
    printf 'Review service stopped unexpectedly.\n' >&2
    exit_for_child "${SERVICE_PID}"
fi

printf 'ngrok stopped unexpectedly.\n' >&2
exit_for_child "${NGROK_PID}"
