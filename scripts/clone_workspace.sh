#!/usr/bin/env bash
# Clone every repo in a GitHub org locally, resumably.
#
#   scripts/clone_workspace.sh <org> [--include-archived]
#
# - shallow (--depth 1 --single-branch): fast, small; the graph/README don't need history
# - skip-existing: re-running only clones what's missing (resumable after Ctrl-C / failure)
# - bounded parallelism (6): fast without tripping GitHub secondary/abuse rate limits
# - failures logged, never fatal — a few dead/permission repos won't stop the run
#
# Dest: ~/gitstuff/<org>/<repo>.  Archived repos are skipped unless --include-archived.
set -uo pipefail

ORG="${1:?usage: clone_workspace.sh <org> [--include-archived]}"
INCLUDE_ARCHIVED="${2:-}"
DEST="$HOME/gitstuff/$ORG"
LIST="$DEST/.repos.txt"
FAIL="$DEST/.clone-failures.txt"
PAR=6

mkdir -p "$DEST"
: > "$FAIL"

FILTER='.[] | select(.archived==false) | .name'
[ "$INCLUDE_ARCHIVED" = "--include-archived" ] && FILTER='.[].name'

echo "[$(date '+%H:%M:%S')] enumerating $ORG ..."
gh api "orgs/$ORG/repos?per_page=100&type=all" --paginate --jq "$FILTER" | sort -u > "$LIST"
TOTAL=$(wc -l < "$LIST" | tr -d ' ')
echo "[$(date '+%H:%M:%S')] $TOTAL repos → cloning (shallow, -P $PAR) into $DEST"

export ORG DEST FAIL
clone_one() {
  local name="$1"
  local dir="$DEST/$name"
  [ -d "$dir/.git" ] && return 0           # already cloned → skip (resumable)
  if ! gh repo clone "$ORG/$name" "$dir" -- --depth 1 --single-branch -q 2>/dev/null; then
    echo "$name" >> "$FAIL"
    rm -rf "$dir"                            # clean a half-clone so a rerun retries it
  fi
}
export -f clone_one

# shellcheck disable=SC2016
xargs -P "$PAR" -I {} bash -c 'clone_one "$@"' _ {} < "$LIST"

CLONED=$(find "$DEST" -maxdepth 2 -name .git -type d | wc -l | tr -d ' ')
FAILED=$(wc -l < "$FAIL" | tr -d ' ')
echo "[$(date '+%H:%M:%S')] done: $CLONED cloned, $FAILED failed (see $FAIL)"
