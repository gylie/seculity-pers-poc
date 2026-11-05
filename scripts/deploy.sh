#!/usr/bin/env bash
# Sync the repo (including the local .env) to the server and (re)start the server profile.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
DIR=/opt/muse
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$HOST")

[ -f .env ] || { echo "Missing .env (copy .env.example and fill it in)"; exit 1; }

"${SSH[@]}" "mkdir -p $DIR && chmod 700 $DIR"
rsync -az --delete --exclude .git --exclude tokens --exclude captures --exclude data \
  -e "ssh -i $KEY -o IdentitiesOnly=yes" ./ "$HOST:$DIR/"
# Only the server profile runs on the VPS; the bridge runs at home (see docker-compose.yml).
"${SSH[@]}" "chmod 600 $DIR/.env && cd $DIR && export COMPOSE_PROFILES=server \
  && if [ -n \"\$(docker compose config --services)\" ]; then docker compose pull -q --ignore-buildable && docker compose up -d --build --remove-orphans; fi \
  && docker compose ps"
