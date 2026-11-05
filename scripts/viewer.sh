#!/usr/bin/env bash
# Forward the avatar viewer (VPS 127.0.0.1:8091: page + WebSocket of pose numbers)
# to http://localhost:8091 on this Mac. No video is ever sent. Ctrl+C to stop.
set -euo pipefail
ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
echo "Open http://localhost:8091"
exec ssh -i "$KEY" -o IdentitiesOnly=yes -o ServerAliveInterval=15 -N -L 127.0.0.1:8091:127.0.0.1:8091 "$HOST"
