"""Caregiver layer: turns the posture stream into events, raises rule-based
alerts, and asks Meta's Model API (Muse Spark) to write plain-language alerts
and summaries.

Only posture events (times, standing / sitting / lying, in or out of view) are
sent to the model. No images, video or joint coordinates ever leave this service.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

META_API_KEY = os.environ.get("META_API_KEY", "")
META_BASE_URL = os.environ.get("META_BASE_URL", "https://api.meta.ai/v1").rstrip("/")   # OpenAI-compatible
META_MODEL = os.environ.get("META_MODEL", "muse-spark-1.3")
META_REASONING = os.environ.get("META_REASONING", "low")   # Muse Spark reasons before answering
CARE_TZ = ZoneInfo(os.environ.get("CARE_TZ", "America/New_York"))
CARE_NAME = os.environ.get("CARE_NAME", "the resident")
ABSENT_S = float(os.environ.get("ABSENT_S", "10"))         # out of view this long = "left view"
FALL_WINDOW_S = float(os.environ.get("FALL_WINDOW_S", "2.5"))  # upright to lying faster than this = possible fall
GAP_FALL_S = float(os.environ.get("GAP_FALL_S", "15"))     # vanished upright, back lying within this = possible fall
LONG_LIE_S = float(os.environ.get("LONG_LIE_S", "120"))    # lying continuously this long = check-in alert
EVENT_LOG = os.environ.get("EVENT_LOG", "/data/events.jsonl")

SYSTEM_PROMPT = (
    "You help family caregivers of an older adult who lives alone. A home sensor reports only "
    "posture events (standing, sitting, lying, in or out of the camera's view) with times. "
    "Write for a worried family member: warm, plain, short. Use only the facts given; never "
    "invent activities, places or health conditions, and never diagnose. Say 'possible' when "
    "the sensor is guessing. Use 12-hour times. Do not use en dashes or em dashes."
)


def clock(t):
    return datetime.fromtimestamp(t, CARE_TZ).strftime("%-I:%M:%S %p")


def span(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


class MetaModel:
    def __init__(self):
        self.enabled = bool(META_API_KEY)

    def ask(self, prompt, max_tokens=220):
        """max_tokens is for the visible answer; reasoning tokens come on top."""
        if not self.enabled:
            return None, "Meta Model API is not configured (META_API_KEY missing)"
        body = json.dumps({
            "model": META_MODEL, "max_tokens": max_tokens + 2000, "reasoning_effort": META_REASONING,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(f"{META_BASE_URL}/chat/completions", data=body, method="POST", headers={
            "Authorization": f"Bearer {META_API_KEY}", "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            return None, f"Meta Model API error {e.code}: {e.read()[:200].decode(errors='replace')}"
        except (urllib.error.URLError, TimeoutError) as e:
            return None, f"Meta Model API unreachable: {e}"
        text = (data["choices"][0]["message"].get("content") or "").strip()
        if not text:
            return None, f"{META_MODEL} returned no text ({data['choices'][0].get('finish_reason')})"
        text = text.replace("—", ", ").replace("–", " to ")   # house style
        print(f"{META_MODEL} answered in {time.time() - t0:.1f} s", flush=True)
        return text, None


class Caregiver:
    def __init__(self, publish_note):
        self.publish_note = publish_note
        self.model = MetaModel()
        self.events = deque(maxlen=2000)
        self.lock = threading.Lock()
        self.present, self.last_seen = False, 0.0
        self.posture, self.since = None, 0.0
        self.upright_until = 0.0          # last time the person was seen standing
        self.left_upright = False         # vanished while standing
        self.long_lie_sent = False
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        self.log = open(EVENT_LOG, "a", buffering=1)
        state = f"on, {META_MODEL} via {META_BASE_URL}" if self.model.enabled else "off (no META_API_KEY)"
        print(f"caregiver: Meta Model API {state}, timezone {CARE_TZ.key}", flush=True)

    # ---------- events ----------
    def _event(self, t, kind, detail=""):
        ev = {"t": round(t, 3), "kind": kind, "detail": detail}
        with self.lock:
            self.events.append(ev)
        self.log.write(json.dumps(ev) + "\n")
        self.publish_note({"type": "event", "time": clock(t), **ev})

    def update(self, t, seen, posture):
        """Called once per processed frame; cheap, never blocks on the network."""
        if seen:
            if not self.present:
                self.present = True
                came_back_lying = posture == "lying"
                self._event(t, "entered view", posture or "")
                if came_back_lying and self.left_upright and t - self.last_seen < GAP_FALL_S:
                    self._alert(t, "possible_fall", f"was standing, went out of view for {span(t - self.last_seen)}, "
                                                    "and reappeared lying down")
            self.last_seen = t
            if posture and posture != self.posture:
                prev, prev_since = self.posture, self.since
                self.posture, self.since = posture, t
                self.long_lie_sent = False
                self._event(t, posture, f"after {prev} for {span(t - prev_since)}" if prev else "")
                if posture == "lying" and t - self.upright_until < FALL_WINDOW_S:
                    self._alert(t, "possible_fall", f"went from standing to lying down in {t - self.upright_until:.1f} s")
            if posture == "standing":
                self.upright_until = t
            if posture == "lying" and not self.long_lie_sent and t - self.since >= LONG_LIE_S:
                self.long_lie_sent = True
                self._alert(t, "long_lie", f"has been lying down for {span(t - self.since)}")
        elif self.present and t - self.last_seen >= ABSENT_S:
            self.present = False
            self.left_upright = self.posture == "standing"
            self._event(self.last_seen, "left view", self.posture or "")

    # ---------- alerts ----------
    def _alert(self, t, kind, fact):
        title = {"possible_fall": "Possible fall", "long_lie": "Lying down for a long time"}[kind]
        note = {"type": "alert", "kind": kind, "title": title, "time": clock(t), "t": round(t, 3),
                "fact": f"{CARE_NAME.capitalize()} {fact}.", "text": None}
        self._event(t, "alert", f"{title}: {fact}")
        self.publish_note(dict(note))   # rule-based alert goes out at once; the model's wording follows

        def write():
            recent = self.timeline(t - 15 * 60, t)
            text, err = self.model.ask(
                f"ALERT at {clock(t)}: {title}. The sensor says {CARE_NAME} {fact}.\n"
                f"Recent events:\n{recent}\n\n"
                "Write the push notification a family caregiver receives: one sentence saying what "
                "happened and when, then one short suggestion (for example, call to check in). "
                "Under 45 words.", max_tokens=120)
            self.publish_note({**note, "text": text, "error": err})
        threading.Thread(target=write, daemon=True).start()

    # ---------- summaries ----------
    def timeline(self, start, end):
        with self.lock:
            evs = [e for e in self.events if start <= e["t"] <= end]
        if not evs:
            return "(no events)"
        return "\n".join(f"{clock(e['t'])}  {e['kind']}" + (f" ({e['detail']})" if e["detail"] else "") for e in evs)

    def totals(self, start, end):
        """Seconds spent in each posture and out of view between start and end."""
        with self.lock:
            evs = [e for e in self.events if e["t"] <= end]
        out, state, t_prev = {}, None, start
        for e in evs:
            kind = e["kind"]
            if kind not in ("standing", "sitting", "lying", "left view", "entered view"):
                continue
            t = max(e["t"], start)
            if state and t > t_prev:
                out[state] = out.get(state, 0) + t - t_prev
            state = "out of view" if kind == "left view" else (e["detail"] or state if kind == "entered view" else kind)
            t_prev = t
        if state and end > t_prev:
            out[state] = out.get(state, 0) + end - t_prev
        return {k: round(v) for k, v in out.items() if v >= 1}

    def summary(self, hours=12.0):
        end = time.time()
        start = end - hours * 3600
        with self.lock:
            first = next((e["t"] for e in self.events if e["t"] >= start), None)
        if first is None:
            return {"ok": False, "text": None, "error": "No activity recorded yet."}
        totals = self.totals(first, end)
        facts = (f"Report period: {clock(first)} to {clock(end)}.\n"
                 f"Time per state: " + ", ".join(f"{k} {span(v)}" for k, v in totals.items()) + "\n"
                 f"Events:\n{self.timeline(start, end)}")
        text, err = self.model.ask(
            f"{facts}\n\nWrite a short update for the family about {CARE_NAME}: how the period went, "
            "anything that deserves a check-in (alerts, long lying, long time out of view), and "
            "reassurance where things look normal. 3 to 5 sentences.", max_tokens=260)
        return {"ok": text is not None, "text": text, "error": err, "totals": totals,
                "from": clock(first), "to": clock(end), "model": META_MODEL}

    def recent_notes(self, n=8):
        with self.lock:
            evs = list(self.events)[-n:]
        return [{"type": "event", "time": clock(e["t"]), **e} for e in evs]
