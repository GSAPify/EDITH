#!/bin/bash
# edithd launchd wrapper (spec: EDITH operationalization item 1).
#
# launchd does NOT source .env or any shell rc/profile file, but edithd needs
# BIFROST_BASE_URL, BIFROST_API_KEY, ELEVENLABS_*, and EDITH_WAKE_MODEL in its
# environment. This wrapper sources the repo's untracked .env (never committed
# — see .env.example) and then execs the daemon so those vars are inherited.
#
# No secret ever lands in a tracked file: this script only reads .env at
# runtime, it does not contain any credential itself.
#
# Self-locates the repo directory from its own path, so it needs no placeholder
# substitution. Only the plist (deploy/com.gsapify.edithd.plist) needs one —
# the absolute path launchd uses to invoke this script in the first place.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

cd "${REPO_DIR}"
exec "${REPO_DIR}/.venv/bin/python" -m edith.daemon
