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
# history for five books, and the forward out-of-sample record on the only
# confirmed edge. Local backups on the same box do not survive losing the box.
#
# Secrets never travel: backup_state.py excludes .env and config.ini from the
# archive by construction, and this script re-checks before keeping a copy.
set -euo pipefail

HOST="${FUND_HOST:-root@your-fund-host}"
REMOTE_DIR="/opt/fund"
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
if [ -f "$DEST/$name" ]; then
    echo "already have $name"
else
    scp -q "$HOST:$latest" "$DEST/$name.part"
    # refuse to keep an archive that carries a secret
    if tar -tzf "$DEST/$name.part" | grep -qE '(^|/)\.env$|config\.ini$'; then
        rm -f "$DEST/$name.part"
        echo "REFUSED $name: archive contains a secret file" >&2
        exit 2
    fi
    tar -tzf "$DEST/$name.part" | grep -q 'state\.json$' || {
        rm -f "$DEST/$name.part"
        echo "REFUSED $name: no state.json inside, that is not a usable backup" >&2
        exit 3
    }
    mv "$DEST/$name.part" "$DEST/$name"
    echo "pulled $name ($(du -h "$DEST/$name" | cut -f1))"
fi

# keep the last N, drop the rest
ls -t "$DEST"/fund_ledgers_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
    echo "pruned $(basename "$old")"
done
