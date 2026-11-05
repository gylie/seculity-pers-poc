"""Pose service: RTSP stream -> YOLOv8n person tracking -> MediaPipe Pose keypoints.

Milestone 2 logs keypoints per frame as JSON lines. With DEBUG_VIEW=1 it also
serves an MJPEG skeleton overlay on 127.0.0.1 for development only: that view
shows camera video and must never ship.
"""
import atexit
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import shared_memory

import cv2
import mediapipe as mp
import numpy as np
import torch
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision
from ultralytics import YOLO

from caregiver import Caregiver
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.sync.server import serve as ws_serve

RTSP_URL = os.environ.get("RTSP_URL", "rtsp://127.0.0.1:8554/living-room-cam")
YOLO_EVERY = int(os.environ.get("YOLO_EVERY", "1"))      # run detection every N frames
YOLO_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "320"))
YOLO_CONF = float(os.environ.get("YOLO_CONF", "0.25"))
HOLD_S = float(os.environ.get("HOLD_S", "1.0"))          # keep posing on the last box after a missed detection
POSE_MODEL = os.environ.get("POSE_MODEL", "lite")        # lite | full
CROP_PAD = float(os.environ.get("CROP_PAD", "0.2"))      # padding around the person box
KEYPOINT_LOG = os.environ.get("KEYPOINT_LOG", "/data/keypoints.jsonl")
DEBUG_VIEW = os.environ.get("DEBUG_VIEW", "0") == "1"
DEBUG_PORT = int(os.environ.get("DEBUG_PORT", "8090"))
STATS_EVERY = float(os.environ.get("STATS_EVERY", "5"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "15"))       # frames waiting before the oldest are dropped
VIEWER_PORT = int(os.environ.get("VIEWER_PORT", "8091"))  # avatar viewer + WebSocket, 127.0.0.1 only
VIEWER_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "index.html")

# Joints sent to the viewer (MediaPipe's left/right are the person's own).
JOINTS = {"nose": 0, "l_ear": 7, "r_ear": 8, "l_shoulder": 11, "r_shoulder": 12, "l_elbow": 13, "r_elbow": 14,
          "l_wrist": 15, "r_wrist": 16, "l_hip": 23, "r_hip": 24, "l_knee": 25, "r_knee": 26,
          "l_ankle": 27, "r_ankle": 28, "l_foot": 31, "r_foot": 32}

# BlazePose body connections (face points left out).
SKELETON = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24),
            (23, 25), (25, 27), (27, 29), (29, 31), (27, 31), (24, 26), (26, 28), (28, 30),
            (30, 32), (28, 32)]


class FrameQueue:
    """Receives frames from frame_reader.py, a separate process that decodes the
    stream into a shared-memory ring. Reading in this process lost frames while
    detection and pose held the GIL; now only a short copy per frame happens here.
    Frames arrive in bursts, so every queued frame is processed in order; past
    MAX_QUEUE the oldest are dropped so slow processing cannot build up latency."""

    SLOTS, SLOT_BYTES = 8, 1920 * 1080 * 3   # up to 1080p; 8 slots = 0.5 s of slack

    def __init__(self, url):
        self.url = url
        self.cond = threading.Condition()
        self.queue, self.dropped, self.received = deque(), 0, 0
        self.shm = shared_memory.SharedMemory(create=True, size=self.SLOTS * self.SLOT_BYTES)
        atexit.register(self.shm.unlink)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        reader = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frame_reader.py")
        while True:
            proc = subprocess.Popen([sys.executable, reader, self.url, self.shm.name, str(self.SLOTS),
                                     str(self.SLOT_BYTES)], stdout=subprocess.PIPE, text=True)
            first = proc.stdout.readline().split()
            if first[:1] != ["size"]:
                proc.wait()
                print(f"stream not available at {self.url}, retrying", flush=True)
                time.sleep(2)
                continue
            w, h = int(first[1]), int(first[2])
            print(f"stream open: {self.url} ({w}x{h})", flush=True)
            for line in proc.stdout:
                slot, t_recv = line.split()
                # The reader ships raw yuv420p (half the bytes of BGR, no conversion in ffmpeg)
                yuv = np.frombuffer(self.shm.buf, np.uint8, w * h * 3 // 2, int(slot) * self.SLOT_BYTES)
                frame = cv2.cvtColor(yuv.reshape(h * 3 // 2, w), cv2.COLOR_YUV2BGR_I420)
                with self.cond:
                    self.queue.append((frame, float(t_recv)))
                    self.received += 1
                    while len(self.queue) > MAX_QUEUE:
                        self.queue.popleft()
                        self.dropped += 1
                    self.cond.notify()
            proc.wait()
            print("stream ended, reconnecting", flush=True)
            time.sleep(1)

    def get(self):
        """Next frame, its arrival time, and frames dropped since the last call."""
        with self.cond:
            self.cond.wait_for(lambda: self.queue)
            frame, t_recv = self.queue.popleft()
            dropped, self.dropped = self.dropped, 0
            return frame, t_recv, dropped


class DebugView:
    """MJPEG skeleton overlay on 127.0.0.1, encoded only while someone watches."""

    def __init__(self, port):
        self.cond = threading.Condition()
        self.jpeg, self.seq, self.clients = None, 0, 0
        view = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
                self.end_headers()
                view.clients += 1
                seq = 0
                try:
                    while True:
                        with view.cond:
                            view.cond.wait_for(lambda: view.seq > seq)
                            jpeg, seq = view.jpeg, view.seq
                        self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    view.clients -= 1

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"debug view on http://127.0.0.1:{port} (dev only)", flush=True)

    def publish(self, frame, people, primary, keypoints):
        if not self.clients:
            return
        img = frame.copy()
        h, w = img.shape[:2]
        for p in people:
            x1, y1, x2, y2 = (int(v) for v in p["box"])
            color = (0, 200, 255) if p is primary else (160, 160, 160)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)
            cv2.putText(img, f"id {p['id']}", (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
        if keypoints:
            pts = [(int(k[0] * w), int(k[1] * h), k[3]) for k in keypoints]
            for a, b in SKELETON:
                if pts[a][2] > 0.5 and pts[b][2] > 0.5:
                    cv2.line(img, pts[a][:2], pts[b][:2], (80, 255, 80), 2)
            for x, y, vis in pts[11:]:
                if vis > 0.5:
                    cv2.circle(img, (x, y), 2, (255, 255, 255), -1)
        if w > 960:   # encoding full 1080p would eat the CPU the pipeline needs
            img = cv2.resize(img, (960, int(h * 960 / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self.cond:
                self.jpeg, self.seq = buf.tobytes(), self.seq + 1
                self.cond.notify_all()


class PoseHub:
    """Serves the avatar viewer page and a WebSocket of pose packets on one
    127.0.0.1 port. Only numbers leave this service: no image data is ever sent."""

    def __init__(self, port, html_path):
        self.cond = threading.Condition()
        self.msg, self.seq = None, 0
        self.notes = []        # caregiver events and alerts: every one is delivered, not just the latest
        self.routes = {}       # path -> fn(query dict) -> JSON-able dict
        self.backlog = lambda: []
        html = open(html_path, encoding="utf-8").read()
        logo = open(os.path.join(os.path.dirname(html_path), "seculity-logo.png"), "rb").read()

        def process_request(conn, request):
            path = request.path.split("?")[0]
            if path == "/ws":
                return None   # continue with the WebSocket handshake
            if path in ("/", "/index.html"):
                resp = conn.respond(200, html)
                del resp.headers["Content-Type"]
                resp.headers["Content-Type"] = "text/html; charset=utf-8"
                resp.headers["Cache-Control"] = "no-store"
                return resp
            if path == "/seculity-logo.png":
                return Response(200, "OK", Headers([("Content-Type", "image/png"),
                                                    ("Content-Length", str(len(logo))),
                                                    ("Cache-Control", "max-age=3600")]), logo)
            if path in self.routes:
                query = dict(kv.split("=", 1) for kv in request.path.partition("?")[2].split("&") if "=" in kv)
                body = json.dumps(self.routes[path](query)).encode()
                return Response(200, "OK", Headers([("Content-Type", "application/json"),
                                                    ("Content-Length", str(len(body))),
                                                    ("Cache-Control", "no-store")]), body)
            return conn.respond(404, "not found\n")

        def handler(conn):
            for note in self.backlog():
                conn.send(json.dumps(note, separators=(",", ":")))
            with self.cond:
                seq, n_notes = self.seq, len(self.notes)
            try:
                while True:
                    with self.cond:
                        if not self.cond.wait_for(lambda: self.seq > seq or len(self.notes) > n_notes, timeout=10):
                            continue
                        notes, n_notes = self.notes[n_notes:], len(self.notes)
                        msg = self.msg if self.seq > seq else None
                        seq = self.seq
                    for note in notes:
                        conn.send(note)
                    if msg:
                        conn.send(msg)
            except ConnectionClosed:
                pass

        server = ws_serve(handler, "127.0.0.1", port, process_request=process_request, compression=None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"avatar viewer on http://127.0.0.1:{port}", flush=True)

    def note(self, note):
        with self.cond:
            self.notes.append(json.dumps(note, separators=(",", ":")))
            self.cond.notify_all()

    def publish(self, packet):
        with self.cond:
            self.msg, self.seq = json.dumps(packet, separators=(",", ":")), self.seq + 1
            self.cond.notify_all()


def classify_posture(kp, world, box, w, h):
    """standing / sitting / lying from one frame, or None when unsure.
    Lying: the torso is far from upright in the image, or the box is wide.
    Sitting: the thigh folds toward the torso (3D angle from world landmarks)."""
    def mid(a, b):
        return [(a[i] + b[i]) / 2 for i in range(3)]
    sh_x, sh_y = (kp[11][0] + kp[12][0]) / 2 * w, (kp[11][1] + kp[12][1]) / 2 * h
    hp_x, hp_y = (kp[23][0] + kp[24][0]) / 2 * w, (kp[23][1] + kp[24][1]) / 2 * h
    tilt = math.degrees(math.atan2(abs(sh_x - hp_x), hp_y - sh_y))   # 0 = shoulders straight above hips
    bw, bh = box[2] - box[0], box[3] - box[1]
    if tilt > 60 or bw > 1.3 * bh:
        return "lying"
    side = 25 if kp[25][3] >= kp[26][3] else 26
    if kp[side][3] < 0.4:
        return None   # knees hidden: keep the last posture
    hip = world[side - 2]
    thigh = [world[side][i] - hip[i] for i in range(3)]
    torso = [a - b for a, b in zip(mid(world[11], world[12]), mid(world[23], world[24]))]
    dot = sum(a * b for a, b in zip(thigh, torso))
    norm = math.sqrt(sum(a * a for a in thigh) * sum(b * b for b in torso)) or 1
    angle = math.degrees(math.acos(max(-1, min(1, dot / norm))))   # ~180 standing, ~90 sitting
    return "sitting" if angle < 135 else "standing"


class PostureFilter:
    """Changes posture only after the same new reading 4 frames in a row."""

    def __init__(self, frames=4):
        self.state, self.cand, self.count, self.frames = None, None, 0, frames

    def update(self, raw):
        if raw is None or raw == self.state:
            self.cand = None
            return self.state
        self.count = self.count + 1 if raw == self.cand else 1
        self.cand = raw
        if self.state is None or self.count >= self.frames:
            self.state, self.cand = raw, None
        return self.state


def detect_people(model, frame):
    res = model.track(frame, persist=True, classes=[0], imgsz=YOLO_IMGSZ, conf=YOLO_CONF, verbose=False,
                      tracker="bytetrack.yaml")[0]
    people = []
    if res.boxes is not None and len(res.boxes):
        ids = res.boxes.id.int().tolist() if res.boxes.id is not None else [None] * len(res.boxes)
        for box, conf, tid in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), ids):
            people.append({"id": tid, "box": box, "conf": round(conf, 3)})
    return people


def pick_primary(people, last_id):
    """Stay on the same track while it exists, otherwise take the largest person."""
    for p in people:
        if last_id is not None and p["id"] == last_id:
            return p
    if not people:
        return None
    return max(people, key=lambda p: (p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]))


def square_crop(frame, box):
    """Square crop around the person box, padded with black past the frame edge.
    MediaPipe projects landmarks correctly only for square inputs. Returns the
    crop and its top-left corner and side length in frame pixels."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    side = int(max(x2 - x1, y2 - y1) * (1 + 2 * CROP_PAD))
    sx, sy = int((x1 + x2 - side) / 2), int((y1 + y2 - side) / 2)
    crop = frame[max(0, sy):min(h, sy + side), max(0, sx):min(w, sx + side)]
    crop = cv2.copyMakeBorder(crop, max(0, -sy), max(0, sy + side - h), max(0, -sx), max(0, sx + side - w),
                              cv2.BORDER_CONSTANT)
    return crop, sx, sy, side


def main():
    # Linux nice is per thread: lower only this inference thread so the frame reader
    # (its own thread and process, both still at nice 0) always wins the single vCPU.
    os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    torch.set_num_threads(1)
    cv2.setNumThreads(1)
    yolo = YOLO("/models/yolov8n.pt")
    landmarker = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=f"/models/pose_landmarker_{POSE_MODEL}.task"),
        running_mode=vision.RunningMode.VIDEO, num_poses=1))
    debug = DebugView(DEBUG_PORT) if DEBUG_VIEW else None
    hub = PoseHub(VIEWER_PORT, VIEWER_HTML)
    care = Caregiver(hub.note)
    hub.backlog = care.recent_notes
    hub.routes["/summary"] = lambda q: care.summary(float(q.get("hours", "12")))
    hub.routes["/events"] = lambda q: {"events": care.recent_notes(int(q.get("n", "30")))}
    posture = PostureFilter()
    stream = FrameQueue(RTSP_URL)
    os.makedirs(os.path.dirname(KEYPOINT_LOG), exist_ok=True)
    log = open(KEYPOINT_LOG, "w", buffering=1)
    print(f"yolov8n imgsz={YOLO_IMGSZ} every={YOLO_EVERY} conf={YOLO_CONF}, pose={POSE_MODEL}, log={KEYPOINT_LOG}", flush=True)

    n, people, primary_id, last_ts, held, t_seen = 0, [], None, 0, None, 0.0
    stats = {"frames": 0, "skipped": 0, "pose_hits": 0, "yolo_ms": 0.0, "pose_ms": 0.0, "lag_ms": 0.0}
    t_stats = time.time()
    while True:
        frame, t_recv, dropped = stream.get()
        stats["skipped"] += dropped
        h, w = frame.shape[:2]

        t0 = time.perf_counter()
        if n % YOLO_EVERY == 0:
            people = detect_people(yolo, frame)
            stats["yolo_ms"] += (time.perf_counter() - t0) * 1000
        n += 1
        primary = pick_primary(people, primary_id)
        if primary:
            held, t_seen = primary, t_recv
        elif held and t_recv - t_seen < HOLD_S:
            primary = held   # detector missed: keep posing on the last box for a moment
        else:
            held = None
        primary_id = primary["id"] if primary else None

        keypoints, world = None, None
        t1 = time.perf_counter()
        if primary:
            crop, sx, sy, side = square_crop(frame, primary["box"])
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            ts = max(int(t_recv * 1000), last_ts + 1)   # VIDEO mode needs increasing timestamps
            last_ts = ts
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(crop)), ts)
            if result.pose_landmarks:
                keypoints = [[round((sx + lm.x * side) / w, 4), round((sy + lm.y * side) / h, 4),
                              round(lm.z, 4), round(lm.visibility, 3)] for lm in result.pose_landmarks[0]]
                world = [[round(lm.x, 3), round(lm.y, 3), round(lm.z, 3), round(lm.visibility, 3)]
                         for lm in result.pose_world_landmarks[0]]
                stats["pose_hits"] += 1
        stats["pose_ms"] += (time.perf_counter() - t1) * 1000

        box_n = [round(v / s, 4) for v, s in zip(primary["box"], (w, h, w, h))] if primary else None
        state = posture.update(classify_posture(keypoints, world, primary["box"], w, h)) if keypoints else posture.state
        packet = {"timestamp": round(t_recv, 3), "present": keypoints is not None, "posture": state}
        if keypoints:
            # joints: MediaPipe world landmarks, meters, origin between the hips, y down
            packet["joints"] = {name: world[i] for name, i in JOINTS.items()}
            packet["position"] = [round((keypoints[23][0] + keypoints[24][0]) / 2, 4), box_n[3]]   # hip x, feet y
            packet["scale"] = round(box_n[3] - box_n[1], 4)   # box height, a rough distance cue
        hub.publish(packet)
        care.update(t_recv, keypoints is not None, state)

        t_done = time.time()
        stats["lag_ms"] += (t_done - t_recv) * 1000
        stats["frames"] += 1
        log.write(json.dumps({
            "t": round(t_recv, 3), "frame": n, "people": len(people),
            "track_id": primary_id, "box": box_n, "posture": state,
            "keypoints": keypoints, "proc_ms": round((t_done - t_recv) * 1000, 1)}) + "\n")
        if debug:
            debug.publish(frame, people, primary, keypoints)

        if t_done - t_stats >= STATS_EVERY:
            f = max(1, stats["frames"])
            yolo_runs = max(1, -(-f // YOLO_EVERY))
            with stream.cond:
                received, stream.received = stream.received, 0
            print(f"in {received / (t_done - t_stats):4.1f} fps | out {f / (t_done - t_stats):4.1f} fps | skipped {stats['skipped']:3d} | people {len(people)} "
                  f"| pose {100 * stats['pose_hits'] / f:3.0f}% | yolo {stats['yolo_ms'] / yolo_runs:5.1f} ms "
                  f"| pose {stats['pose_ms'] / f:5.1f} ms | lag {stats['lag_ms'] / f:5.1f} ms", flush=True)
            stats = dict.fromkeys(stats, 0)
            t_stats = t_done


if __name__ == "__main__":
    main()
