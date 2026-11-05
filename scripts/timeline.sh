#!/usr/bin/env bash
# Milestone 4: summarize the last N seconds of the keypoint log in 5 s buckets
# (frames, fps, people detected, pose found, posture), plus VPS CPU over the
# window when run with --watch (records live for N seconds first).
#   scripts/timeline.sh 60            the last 60 s already logged
#   scripts/timeline.sh --watch 60    record the next 60 s, then summarize
#   SNAP=name scripts/timeline.sh --watch 60
#       also saves a 480 px snapshot every 5 s from the home bridge into
#       captures/m4/ (gitignored, dev only) to check what really happened
set -euo pipefail
ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$HOST")

if [[ "${1:-}" == "--watch" ]]; then
  secs="${2:-60}"
  echo "recording ${secs} s from $(date +%H:%M:%S)"
  if [[ -n "${SNAP:-}" ]]; then
    mkdir -p "$(dirname "$0")/../captures/m4"
    timeout -s INT "$secs" ffmpeg -loglevel error -rtsp_transport tcp -i "rtsp://127.0.0.1:8554/${CAMERA_NAME:-living-room-cam}" \
      -vf "fps=1/5,scale=640:-1" \
      "$(dirname "$0")/../captures/m4/${SNAP}_%02d.jpg" &
  fi
  "${SSH[@]}" "head -1 /proc/stat > /tmp/tl1; sleep $secs; head -1 /proc/stat > /tmp/tl2; paste /tmp/tl1 /tmp/tl2 | awk '{t=0; for(i=2;i<=11;i++) t+=\$(i+11)-\$i; print \"VPS CPU busy:\", int(100-100*(\$16-\$5)/t) \"%\"}'"
else
  secs="${1:-60}"
fi
wait

PY=$(cat <<'PYEOF'
import json, sys, time
from collections import Counter
secs = float(sys.argv[1])
rows = [json.loads(l) for l in sys.stdin if l.strip()]
end = rows[-1]["t"]
rows = [r for r in rows if r["t"] > end - secs]
start = rows[0]["t"]
buckets = {}
for r in rows:
    buckets.setdefault(int((r["t"] - start) // 5), []).append(r)
print("time      fps  people  pose%  posture")
for k, rs in sorted(buckets.items()):
    pose = sum(1 for r in rs if r["keypoints"])
    post = Counter(r["posture"] for r in rs if r["keypoints"])
    clock = time.strftime("%H:%M:%S", time.localtime(rs[0]["t"]))
    people = max(r["people"] for r in rs)
    print(f"{clock}  {len(rs) / 5:4.1f}  {people:^6}  {100 * pose / len(rs):4.0f}  "
          + ", ".join(f"{p} {n}" for p, n in post.most_common()))
pose = sum(1 for r in rows if r["keypoints"])
print(f"total {len(rows)} frames in {end - start:.0f} s ({len(rows) / max(end - start, 1):.1f} fps), pose found in {100 * pose / len(rows):.0f}%")
PYEOF
)
"${SSH[@]}" "tail -n $((secs * 20)) /opt/muse/data/keypoints.jsonl" | python3 -c "$PY" "$secs"
