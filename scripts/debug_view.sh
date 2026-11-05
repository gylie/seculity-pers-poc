#!/usr/bin/env bash
# Dev only: forward the pose service's skeleton overlay (VPS 127.0.0.1:8090) to
# http://localhost:8090 on this Mac. Needs DEBUG_VIEW=1 in .env. Ctrl+C to stop.
set -euo pipefail
ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
echo "Open http://localhost:8090"
exec ssh -i "$KEY" -o IdentitiesOnly=yes -N -L 127.0.0.1:8090:127.0.0.1:8090 "$HOST"
