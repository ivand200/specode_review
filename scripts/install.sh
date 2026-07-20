#!/bin/sh
set -eu

if [ "$#" -ne 2 ] || [ "$1" != "--release" ]; then
    echo "usage: sudo ./scripts/install.sh --release v0.1.0" >&2
    exit 2
fi

release_tag=$2
case "$release_tag" in
    v[0-9]*.[0-9]*.[0-9]*) ;;
    *)
        echo "installation failed: release_tag" >&2
        exit 1
        ;;
esac

application_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$application_root"

checked_out_tag=$(git describe --tags --exact-match HEAD 2>/dev/null) || {
    echo "installation failed: exact_release" >&2
    exit 1
}
if [ "$checked_out_tag" != "$release_tag" ]; then
    echo "installation failed: exact_release" >&2
    exit 1
fi

exec uv run --frozen --no-dev python -m specode_review.installation \
    --release "$release_tag"
