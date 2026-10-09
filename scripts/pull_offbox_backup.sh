#!/bin/bash
# Pull the fund's newest ledger archive off the trading host to this machine.
#
# Pull, not push, on purpose. The backup script supports a remote_cmd that would
# have the server send archives somewhere, but pointing that at this laptop
# means giving the server an inbound path to it. That inverts the trust
# direction for no gain: this machine already reaches the server, so the copy
# can be taken from the side that already has the right.
#
# Everything the fund knows lives in one file on one host: 45 days of equity
# history for five books and forward research records. Local backups on the
# same box do not survive losing the box; historic labels are not current proof.
#
# Credential files are excluded by backup_state.py and re-checked here. The
# financial records inside a backup remain private operator data.
set -euo pipefail

HOST="${FUND_HOST:?Set FUND_HOST explicitly to the reviewed backup source.}"
REMOTE_DIR="${FUND_REMOTE_DIR:?Set FUND_REMOTE_DIR explicitly to the fund directory.}"
if [[ ! "$HOST" =~ ^([a-zA-Z_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]; then
    printf '%s\n' 'FUND_HOST must be a hostname/IPv4 address with an optional username.' >&2
    exit 2
fi
if [[ ! "$REMOTE_DIR" =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
    printf '%s\n' 'FUND_REMOTE_DIR must be an absolute path without shell metacharacters.' >&2
    exit 2
fi
DEST="${FUND_BACKUP_DIR:-$HOME/Trading_backups}"
KEEP=30

mkdir -p "$DEST"

latest=$(ssh -o ConnectTimeout=20 -o BatchMode=yes "$HOST" \
    "ls -t $REMOTE_DIR/data/backups/*.tar.gz 2>/dev/null | head -1")
if [ -z "$latest" ]; then
    echo "no archive found on $HOST" >&2
    exit 1
fi

name=$(basename "$latest")
if [[ "$latest" != "$REMOTE_DIR/data/backups/"* || ! "$name" =~ ^fund_ledgers_[a-zA-Z0-9_.-]+\.tar\.gz$ ]]; then
    printf '%s\n' 'REFUSED unexpected archive location or name.' >&2
    exit 2
fi
if [ -f "$DEST/$name" ]; then
    echo "already have $name"
else
    incoming=$(mktemp "$DEST/.incoming.XXXXXX")
    trap 'rm -f -- "$incoming"' EXIT
    scp -q "$HOST:$latest" "$incoming"
    # Read the complete inventory once. Under pipefail, tar | grep -q can
    # report a broken pipe after an early match and bypass the secret guard.
    inventory=$(tar -tzf "$incoming")
    # refuse to keep an archive that carries a secret
    if grep -qE '(^|/)\.env($|\.)|(^|/)[^/]+\.env$|(^|/)config\.ini$' <<< "$inventory"; then
        echo "REFUSED $name: archive contains a secret file" >&2
        exit 2
    fi
    grep -qE '(^|/)state\.json$' <<< "$inventory" || {
        echo "REFUSED $name: no state.json inside, that is not a usable backup" >&2
        exit 3
    }
    chmod 600 "$incoming"
    mv "$incoming" "$DEST/$name"
    trap - EXIT
    echo "pulled $name ($(du -h "$DEST/$name" | cut -f1))"
fi

# keep the last N, drop the rest
ls -t "$DEST"/fund_ledgers_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
    echo "pruned $(basename "$old")"
done
