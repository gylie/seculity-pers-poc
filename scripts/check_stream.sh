#!/usr/bin/env bash
# Milestone 1 checks, run from this machine against the server:
#   probe     ffprobe the RTSP stream
#   soak [s]  decode the stream for s seconds (default 600) and report speed/errors
#   latency   decode 4 s, keep the last frame and print the server time it arrived; point the camera
#             at a screen showing an NTP-synced clock with milliseconds (e.g. time.is)
#             and compare the clock in the image to that time
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="$(dirname "$0")/../.env"; [[ -f "$ENV_FILE" ]] || ENV_FILE=.env
HOST="${MUSE_HOST:-$(grep -sE "^MUSE_HOST=" "$ENV_FILE" | cut -d= -f2)}"
HOST="${HOST:?set MUSE_HOST (user@your-vps) in .env}"
KEY="${MUSE_KEY:-$HOME/.ssh/pers_poc_ed25519}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$HOST")
CAM="$(grep -E '^CAMERA_NAME=' .env | cut -d= -f2)"
URL="rtsp://127.0.0.1:8554/$CAM"
LOCAL_TZ="$(readlink /etc/localtime | sed "s#.*/zoneinfo/##")"   # print server time in this Mac's zone

case "${1:-probe}" in
  probe)
    "${SSH[@]}" "ffprobe -v error -rtsp_transport tcp -show_entries stream=codec_name,width,height,avg_frame_rate -of compact $URL"
    ;;
  soak)
    secs="${2:-600}"
    "${SSH[@]}" "timeout $((secs + 15)) ffmpeg -hide_banner -rtsp_transport tcp -i $URL -t $secs -f null - 2>&1 | grep -E 'frame=|error|Error' | tail -5"
    ;;
  latency)
    mkdir -p captures
    "${SSH[@]}" "ffmpeg -loglevel error -rtsp_transport tcp -i $URL -t 4 -update 1 -y /tmp/muse_frame.jpg && TZ=$LOCAL_TZ date +%H:%M:%S.%3N"
    scp -q -i "$KEY" -o IdentitiesOnly=yes "$HOST:/tmp/muse_frame.jpg" "captures/latency_$(date +%s).jpg"
    echo "Frame saved in captures/. Latency = time above minus the clock shown in the image (both NTP-synced)."
    ;;
  *) echo "usage: $0 probe|soak [seconds]|latency"; exit 1 ;;
esac
