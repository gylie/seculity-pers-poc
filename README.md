# muse: PERS feasibility PoC

Feasibility proof of concept for a privacy-preserving elder-care PERS system.
It answers one question: can we take a consumer camera stream, run person
detection and pose estimation on it, and drive an avatar in a virtual scene
from the pose data in real time?

This is **not** the product architecture. See "PoC shortcuts" below.

## Architecture

```
 ┌──── home LAN (Mac, compose profile "home") ────┐
 │ Wyze camera ──TUTK──► wyze-bridge (RTSP :8554) │
 └───────────────────────────────┬────────────────┘
                                 │ SSH reverse tunnel (scripts/tunnel.sh)
 ┌──────── VPS (compose profile "server") ───────────────────┐
 │                                ▼                                      │
 │  RTSP 127.0.0.1:8554 ─────────────────► pose service (Python, CPU)    │
 │                                         OpenCV decode                 │
 │                                         YOLOv8n person track          │
 │                                         MediaPipe Pose keypoints      │
 │                                              │                        │
 │                                              ▼ WebSocket              │
 │                                  {joints, position, scale, timestamp} │
 │                                              │                        │
 │                                  viewer (static three.js page)        │
 └──────────────────────────────────────────────┼────────────────────────┘
                                                ▼
                              browser: avatar in a garden, no video
```

Milestone status:

1. Server + wyze-bridge + verified RTSP stream (done 2026-11-03, results below)
2. Detection + pose service (done 2026-11-04, results below)
3. Pose to avatar viewer (done 2026-11-04, results below)
4. Measurements and written assessment (done 2026-11-04, see [ASSESSMENT.md](ASSESSMENT.md))

## PoC shortcuts that MUST NOT ship

- Video is processed in the cloud in this PoC. In the product, all video
  processing runs on a physical hub in the home and raw video never leaves it.
- The Wyze camera stays connected to Wyze's cloud in this PoC. In the product,
  cameras are LAN-only with no internet access.
- No viewer auth, no face recognition, no avatar selection in the PoC: one
  hardcoded avatar.
- Point the test camera at yourself, not at anyone else.
- Posture events (times and standing / sitting / lying / out of view, never
  images or joints) are sent to Meta's hosted Model API for wording. The product
  needs the resident's consent, a data processing agreement, and a fallback when
  the API is down; the rule-based alerts must never depend on it.

## Server

- Any Linux VPS with Docker; the PoC ran on 1 vCPU and 3.8 GB RAM, Ubuntu 24.04.
  Set `MUSE_HOST=user@your-vps` in `.env`; the scripts read it from there.
- Every port in `docker-compose.yml` binds to `127.0.0.1`. Nothing is exposed
  to the internet; reach the viewer and debug view over SSH tunnels.
- The bridge only streams the camera named in `CAMERA_FILTER`. The Wyze
  account has other cameras in other people's rooms; they must stay off.

## Milestone 1 results (2026-11-03)

| Check | Result |
|---|---|
| Stream at the VPS | H.264, 640x360, 15 fps (bridge `QUALITY=SD30`, LAN mode) |
| 10 min soak on the VPS | 8,983 of ~9,000 frames, 14.98 fps, 0 dropped, 0 duplicated, 0 decode errors, ran at 1.0x |
| Decode CPU on the VPS | under 1% of the single vCPU at 360p |
| Latency, camera to VPS | 0.5 to 1.7 s (3 frames: camera OSD clock second vs. server arrival time 19:35:13.465 / 19:35:19.639 / 19:35:25.736; assumes the camera's NTP clock is within 1 s) |

The expected 3 to 10 s did not materialize because the bridge reads the camera
over the LAN instead of Wyze's cloud relay; the SSH tunnel adds little.

## Milestone 2 results (2026-11-04)

Settings: bridge `QUALITY=HD120` (1920x1080), YOLOv8n at `imgsz=640` every 4th
frame with ByteTrack in between (`conf=0.15`, 1 s hold), MediaPipe Pose lite on a
square crop around the tracked person. All on the single vCPU, no GPU.

| Check | Result |
|---|---|
| Throughput, empty room | 14.5 to 15 fps in and out, 0 frames skipped |
| Throughput, person moving | 8 to 13 fps, about 11 on average, which is everything the camera delivers (see below); 0 frames skipped, output equals input |
| Processing time | YOLO 70 to 100 ms per run (every 4th frame), pose 12 to 20 ms, frame-in to keypoints-out 40 to 70 ms |
| CPU | 39% busy with the room empty, 43 to 51% with a person in view (whole VPS); ffmpeg decode of 1080p about 6% |
| Pose found when a person is detected | 94 to 100% of frames standing, walking and sitting |
| Lying flat behind the couch back | not detected (YOLO misses the partly hidden, horizontal body in low light). Carry into Milestone 4. |
| 360p (`SD30`) vs 1080p (`HD120`) | at 360p YOLO missed sitting entirely; 1080p is needed for this camera position |

**The camera, not the VPS, sets the frame rate when there is motion.** Counted
by the real clock over the same 30 s of movement: the Mac next to the bridge got
298 frames in 27.6 s (10.8 fps), plain ffmpeg on the VPS 290 in 30 s, and the
pose service the same. The camera keeps stamping frames as 15 fps, so any tool
that stops on stream time (`ffmpeg -t 30`) reports a full 450 frames' worth and
hides the drop (ffmpeg shows `speed=0.73x`). Measure fps by wall clock only.

Design notes:

- `pose/frame_reader.py` runs ffmpeg in its own process and drops raw yuv420p
  frames into an 8-slot shared-memory ring; the service copies each one out and
  converts to BGR. Inference runs at nice 10 so the reader always gets the CPU.
- Keypoints per frame go to `/opt/muse/data/keypoints.jsonl` as
  `{t, frame, people, track_id, box, keypoints[33 x (x, y, z, visibility)], proc_ms}`
  in normalized frame coordinates.

## Milestone 3 results (2026-11-04)

The pose service also serves the viewer: `http://127.0.0.1:8091/` is a three.js
garden page (three 0.186.1 from jsDelivr) and `ws://127.0.0.1:8091/ws` streams one
JSON packet per processed frame. Only numbers leave the service, never pixels:

```json
{"timestamp": 1793835580.4, "present": true, "posture": "sitting",
 "joints": {"l_hip": [0.09, -0.01, 0.02, 0.99], "...": "17 joints, MediaPipe world landmarks: meters, origin between the hips, y down, [x, y, z, visibility]"},
 "position": [0.61, 0.83], "scale": 0.42}
```

`position` is the hip center x and the feet y in normalized frame coordinates
(used for where the avatar stands in the garden), `scale` is the person box
height. Posture is decided on the server: lying when the torso is more than 60
degrees from upright in the image or the box is 1.3x wider than tall; sitting
when the thigh folds toward the torso (under 135 degrees, from 3D landmarks);
a new posture must hold for 4 frames before it is reported. The viewer stands the
torso upright (removing the camera's downward tilt), lays the body flat when
lying, rests it on the grass, and shows a stump under it when sitting and a
blanket when lying.

Test, standing then sitting then lying on the floor in open view, 15 s each:

| Check | Result |
|---|---|
| Standing | shown as Standing; a few brief false "sitting" readings while walking and turning (9 and 12 frames in 30 s) |
| Sitting | Sitting within about 1 s, held steadily; avatar shorter on a stump |
| Lying on the floor | Lying down, avatar flat on a blanket. About 8 s with no person detected while getting down to the floor, then detected and held |
| Packet rate in the browser | 10 to 15.5 per second (follows the camera's frame rate) |
| Delay, frame reaching the VPS to packet in the browser | 53 to 115 ms (the camera to VPS leg of 0.5 to 1.7 s from Milestone 1 comes on top) |

Open it from the Mac with `scripts/viewer.sh`, then http://localhost:8091.

## Caregiver layer: Meta Model API (Muse Spark), 2026-11-04

`pose/caregiver.py` turns the posture stream into events (sat down, lying
down, out of view, back in view) and raises rule-based alerts:

- **Possible fall:** standing to lying in under `FALL_WINDOW_S` (2.5 s), or
  vanishing while standing and reappearing lying within `GAP_FALL_S` (15 s),
  which covers the detection gap measured in Milestone 4.
- **Long lie:** lying continuously for `LONG_LIE_S` (120 s in the PoC).

Alerts go to the viewer at once over the WebSocket. Then `muse-spark-1.3` on
Meta's Model API (`https://api.meta.ai/v1`, OpenAI-compatible chat completions,
`reasoning_effort: low`) rewrites each one as a caregiver notification, and on
request writes a family update from the last 12 hours of events
(`GET /summary`; `GET /events` lists recent events). It sees only the event
timeline, never pixels or joints. Measured: about 11 to 13 s per answer.

Set `META_API_KEY` (starts with `LLM_`) in `.env`; `META_MODEL`, `CARE_NAME`,
`CARE_TZ` and `LONG_LIE_S` are optional. Events are also appended to
`/opt/muse/data/events.jsonl` on the VPS.

## Why the bridge runs at home

From the VPS the bridge logs in to Wyze but every camera connection times out
(`IOTC_ER_TIMEOUT`, in both ANY and P2P mode). So the bridge runs on a Mac on
the camera's LAN and an SSH reverse tunnel exposes its RTSP stream on the VPS
at `127.0.0.1:8554`. Detection, pose and the viewer still run on the VPS.

On the Mac the bridge needs the camera's LAN directly: TUTK finds the camera by
LAN broadcast, which neither Colima's default NAT nor Docker's bridge network
passes (both gave the same timeout). So:

- Colima runs in bridged mode on Wi-Fi (`en0`) and gets its own LAN address
  from the router. First start asks for the Mac password once:

  ```bash
  brew install colima docker docker-compose
  colima start --cpu 2 --memory 2 --disk 20 --vm-type vz --network-address --network-mode bridged --network-interface en0 --network-preferred-route
  ```

- The bridge uses `network_mode: host` inside that VM, with RTSP and the web UI
  bound to `127.0.0.1` and HLS/WebRTC/RTMP/SRT off, so the stream is not
  reachable from other devices on the home LAN. Colima forwards the VM's
  loopback ports to the Mac. The VM's own SSH server does listen on its LAN
  address (key auth only).

## Run

```bash
cp .env.example .env                              # then fill it in
COMPOSE_PROFILES=home docker compose up -d        # on the Mac (Colima bridged): start the bridge
scripts/tunnel.sh                                 # on the Mac: keep running
scripts/deploy.sh                                 # rsync to /opt/muse, start the server profile
```

Milestone 1 checks:

```bash
scripts/check_stream.sh probe        # codec, resolution, fps
scripts/check_stream.sh soak 600     # 10 min decode, look for errors/stalls
scripts/check_stream.sh latency      # frame grab vs. an on-screen clock
```

Milestone 2 (pose service on the VPS):

```bash
ssh -i ~/.ssh/pers_poc_ed25519 "$MUSE_HOST" 'cd /opt/muse && docker compose logs -f pose'   # fps / timing every 5 s
scripts/debug_view.sh                 # dev only: skeleton overlay at http://localhost:8090
scripts/viewer.sh                     # avatar viewer at http://localhost:8091 (numbers only, no video)
scripts/timeline.sh --watch 60        # Milestone 4: record 60 s, then fps / people / pose / posture per 5 s
```

Keypoints are written per frame to `/opt/muse/data/keypoints.jsonl` on the VPS
(gitignored, overwritten on restart). Tuning knobs in `.env`: `YOLO_EVERY`
(run detection every N frames, track in between), `YOLO_IMGSZ`, `POSE_MODEL`
(`lite` or `full`), `DEBUG_VIEW`.

Bridge web UI (for 2FA codes or to see camera names): http://localhost:5050 on
the Mac (5000 is taken by macOS AirPlay Receiver).
