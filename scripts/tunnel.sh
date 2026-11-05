#!/usr/bin/env bash
# Carry the home bridge's RTSP stream to the VPS: 127.0.0.1:8554 on the VPS forwards
# to 127.0.0.1:8554 on this Mac. Reconnects if the link drops. Ctrl+C to stop.
set -uo pipefail

ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

while true; do
  # A dropped link can leave the old session holding the VPS port, which makes
  # every reconnect fail with "remote port forwarding failed". Clear it first.
  "${SSH[@]}" "$HOST" 'for p in $(ss -tlnpH "sport = :8554" | grep -oE "pid=[0-9]+" | cut -d= -f2 | sort -u); do kill $p; done' 2>/dev/null
  echo "$(date '+%H:%M:%S') tunnel up"
  "${SSH[@]}" -N -o ExitOnForwardFailure=yes -R 127.0.0.1:8554:127.0.0.1:8554 "$HOST"
  echo "$(date '+%H:%M:%S') tunnel dropped, retrying in 5 s"
  sleep 5
done
