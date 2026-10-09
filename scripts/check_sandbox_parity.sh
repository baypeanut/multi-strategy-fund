#!/bin/bash
# Run the suite the way the apply gate runs it: from a clean checkout with no
# live data directory.
#
# Why this exists. The sandbox copies the repo to a temp dir and excludes
# `data/`, so a test that reads the live state file passes on the host and goes
# red inside every sandbox. The apply gate rolls back on red, so one such test
# silently disarms EVERY autonomous change until someone notices.
#
# It happened on 2026-08-02. `test_state_path_is_anchored_to_the_repo` called
# load_inputs() against the real data/state.json - present on the server,
# absent from the sandbox. All five proposals Fable produced that night failed
# the gate. None of them were rejected on their own merits, and the digest said
# "sandbox red" for each one, which reads exactly like five bad proposals.
#
# Host-green does not imply gate-green. This closes that gap before a deploy
# rather than after a wasted run.
#
# Usage: scripts/check_sandbox_parity.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# This reads `git archive HEAD`, so it validates the COMMITTED tree - the same
# thing the sandbox copies. Run with uncommitted work in the tree and it
# cheerfully green-lights code you are not about to deploy. I did exactly that
# on 2026-08-02: ran it before committing, got PARITY OK for the old tree, and
# shipped a change that turned a test red on the server.
if ! git -C "$ROOT" diff --quiet HEAD -- 2>/dev/null; then
    echo "WORKING TREE IS DIRTY." >&2
    echo "This checks the COMMITTED tree, which is what the sandbox copies," >&2
    echo "so a green result here says nothing about your uncommitted changes." >&2
    echo "Commit first, then re-run." >&2
    git -C "$ROOT" status --porcelain | head -10 >&2
    exit 3
fi

git -C "$ROOT" archive HEAD | tar -x -C "$WORK"
# core/data is source and ships; the gitignored data/ ledger does not, which is
# precisely the difference the sandbox has and the host does not.
cp -r "$ROOT/core/data" "$WORK/core/" 2>/dev/null || true
ln -s "$ROOT/venv" "$WORK/venv"

if [ -d "$WORK/data" ]; then
    echo "unexpected: data/ is tracked; the sandbox parity assumption is stale" >&2
    exit 2
fi

cd "$WORK"
echo "running the suite with no data/ directory (gate conditions)..."
if ./venv/bin/python -m pytest tests/ -q -p no:cacheprovider; then
    echo "PARITY OK - host-green matches gate-green"
else
    echo "PARITY BROKEN - this suite is green on the host and RED in the gate." >&2
    echo "Every autonomous change would be rolled back until this is fixed." >&2
    exit 1
fi
