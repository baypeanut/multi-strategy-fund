#!/usr/bin/env bash
# Retired deployment helper: it targeted a different repository layout and its
# rsync --delete rules did not protect current ledgers or secrets.
set -euo pipefail
printf '%s\n' 'This legacy deployment helper is retired and performs no changes.' >&2
printf '%s\n' 'Current paper entrypoint: python -m scripts.run_paper_live --no-dashboard' >&2
printf '%s\n' 'Prepare a reviewed deployment and state/secret backup; do not sync or overwrite live records.' >&2
exit 2
