#!/usr/bin/env bash
# Retired bootstrap: obsolete src.main/src.portal entrypoints are not present.
set -euo pipefail
printf '%s\n' 'This legacy bootstrap is retired and performs no host changes.' >&2
printf '%s\n' 'Install this project into an isolated Python environment; current entrypoint is python -m scripts.run_paper_live.' >&2
printf '%s\n' 'Bind the dashboard to localhost or configure authenticated TLS access. Existing production services are unchanged.' >&2
exit 2
