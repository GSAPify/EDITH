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
    # Refuse to source a .env that anyone else can read or write.
    #
    # `source` EXECUTES the file, so a group/world-writable .env is arbitrary code as the
    # owner at every login. And a world-READABLE one leaks BIFROST_API_KEY to any other local
    # account: verified on the build machine, where .env was 0644 and a second account (uid
    # 501) could traverse to it. Checking here rather than only documenting it means the
    # permission cannot silently drift back — a chmod regression fails loudly at boot instead
    # of quietly re-exposing the key.
    env_mode="$(stat -f '%OLp' "${ENV_FILE}")"
    env_owner="$(stat -f '%u' "${ENV_FILE}")"
    if [ "${env_owner}" != "$(id -u)" ] || [ "${env_mode}" != "600" ]; then
        echo "[edithd] refusing to source ${ENV_FILE}: must be mode 600 and owned by" \
             "$(id -un) (found mode ${env_mode}, uid ${env_owner}). Fix with:" \
             "chmod 600 ${ENV_FILE}" >&2
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
fi

cd "${REPO_DIR}"
exec "${REPO_DIR}/.venv/bin/python" -m edith.daemon
