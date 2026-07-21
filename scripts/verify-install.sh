#!/bin/sh
set -eu

if [ "$#" -ne 0 ]; then
    echo "usage: sudo ./scripts/verify-install.sh" >&2
    exit 2
fi

application_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$application_root"

exec uv run --frozen --no-dev python -m specode_review.verification
