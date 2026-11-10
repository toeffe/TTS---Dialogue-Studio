#!/usr/bin/env python3
"""Local-only Paradee 1.3 dialogue server. Binds exclusively to 127.0.0.1."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import base64, io, json, re, sys, threading, time, traceback, wave

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

HOST, PORT = "127.0.0.1", 8765
HERE = Path(__file__).resolve().parent
CHANNEL_FILE = HERE / "channel.txt"
CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,25}$")
tts = None
load_error = None
loading = False
sample_rate = 24000
lock = threading.Lock()
control_cv = threading.Condition()
pending_action = ""
control_gen = 0
last_action = ""
delivered_gen = 0
queue_lock = threading.Lock()
queue_state = {"playing": "", "queue": [], "at": 0.0}
listener_note = True
LISTENER_TTL = 8

# Paradee v1.3 voice IDs. Each voice determines its language/accent.
VOICES = {
    "af_heart": "English (US)", "bf_isabella": "English (UK)",
    "am_puck": "English (US)", "bm_fable": "English (UK)",
    "hf_alpha": "Hindi", "hm_omega": "Hindi",
    "ff_siwis": "French", "em_alex": "Spanish",
}

def read_channel():
    try:
        name = CHANNEL_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return name if CHANNEL_RE.fullmatch(name) else ""

def write_channel(name):
    name = str(name or "").strip().lower()
    if name and not CHANNEL_RE.fullmatch(name):
        raise ValueError("Use 1 to 25 letters, numbers, or underscores.")
    if name:
        CHANNEL_FILE.write_text(name + "\n", encoding="utf-8")
    elif CHANNEL_FILE.exists():
        CHANNEL_FILE.unlink()
    return name

def listener_is_live():
    with queue_lock:
        return queue_state["at"] > 0 and (time.time() - queue_state["at"]) < LISTENER_TTL

def control_snapshot():
    with control_cv:
        return control_gen, last_action

def take_playback(wait):
    global pending_action, delivered_gen
    with control_cv:
        if wait and not pending_action:
            control_cv.wait(20)
        action = pending_action
        pending_action = ""
        if action:
            delivered_gen = control_gen
        return action

def claim_cancel(started):
    global pending_action, delivered_gen
    with control_cv:
        if control_gen == started or delivered_gen == control_gen:
            return ""
        delivered_gen = control_gen
        pending_action = ""
        return last_action

def set_playback(action):
    global pending_action, control_gen, last_action
    action = str(action or "").strip().lower()
    if action not in ("stop", "skip"):
        raise ValueError("Use stop or skip.")
    with control_cv:
        control_gen += 1
        if pending_action != "stop":
            pending_action = action
        last_action = pending_action
        held = pending_action
        control_cv.notify_all()
    print("Stop." if held == "stop" else "Skip.", flush=True)
    if not listener_is_live():
        print("OBS source is not connected. Refresh that browser source.", flush=True)
    return held

def queue_view():
    global listener_note
    announce = False
    with queue_lock:
        live = queue_state["at"] > 0 and (time.time() - queue_state["at"]) < LISTENER_TTL
        if live:
            listener_note = False
        elif queue_state["at"] > 0 and not listener_note:
            listener_note = True
            announce = True
        payload = {
            "playing": queue_state["playing"] if live else "",
            "queue": list(queue_state["queue"]) if live else [],
            "live": live,
            "listener": live,
        }
    if announce:
        print("OBS source is not connected. Refresh that browser source.", flush=True)
    return payload

def clean_entry(item):
    if item is None or item == "":
        return None
    if isinstance(item, str):
        text = item.strip()[:180]
        return {"name": "", "voice": "", "text": text} if text else None
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()[:40]
    voice = str(item.get("voice") or "").strip()[:40]
    text = str(item.get("text") or "").strip()[:180]
    if not (name or voice or text):
        return None
    return {"name": name, "voice": voice, "text": text}

def entry_label(item):
    if not item:
        return ""
    who = item.get("name") or "Chat"
    voice = item.get("voice") or ""
    text = item.get("text") or ""
    head = f"{who} ({voice})" if voice else who
    return f"{head}: {text}" if text else head

def set_queue_state(playing, items):
    global listener_note
    playing = clean_entry(playing)
    if not isinstance(items, list):
        items = []
    clean = []
    for item in items[:40]:
        entry = clean_entry(item)
        if entry:
            clean.append(entry)
    with queue_lock:
        now = time.time()
        was_live = queue_state["at"] > 0 and (now - queue_state["at"]) < LISTENER_TTL
        changed = queue_state["playing"] != playing or queue_state["queue"] != clean
        queue_state["playing"] = playing
        queue_state["queue"] = clean
        queue_state["at"] = now
        if not was_live:
            listener_note = False
    if not was_live:
        print("OBS source connected.", flush=True)
    if changed or not was_live:
        print(f"Playing: {entry_label(playing) or '(idle)'}", flush=True)
        if not clean:
            print("Queue: (empty)", flush=True)
        else:
            shown = clean[:12]
            print("Queue:", flush=True)
            for entry in shown:
                print("  " + entry_label(entry), flush=True)
            if len(clean) > len(shown):
                print(f"  … +{len(clean) - len(shown)}", flush=True)

def initialize():
    global tts, load_error, loading, sample_rate
    with lock:
        if tts is not None or loading:
            return
        loading = True
    try:
        from paradee import Paradee, SAMPLE_RATE
        # Explicitly request v1.3 so we do not silently fall back to the v1.2
        # four-voice checkpoint. This package release must expose v1.3.
        tts = Paradee(version="1.3")
        sample_rate = int(SAMPLE_RATE)
        print(f"Paradee 1.3 loaded ({sample_rate} Hz).", flush=True)
    except Exception as exc:
        load_error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        loading = False

def make_wav(chunks, pauses_ms):
    import numpy as np
    audio_parts = []
    cues = []
    cursor = 0
    for i, audio in enumerate(chunks):
        arr = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
        start = cursor / sample_rate
        audio_parts.append(arr)
        cursor += int(arr.shape[0])
        cues.append({"start": round(start, 3), "end": round(cursor / sample_rate, 3)})
        if i != len(chunks) - 1:
            gap = int(sample_rate * int(pauses_ms[i]) / 1000)
            if gap > 0:
                audio_parts.append(np.zeros(gap, dtype=np.float32))
                cursor += gap
    if not audio_parts:
        raise ValueError("No audio was generated.")
    joined = np.concatenate(audio_parts)
    pcm = (np.clip(joined, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue(), cues

def first_value(bag, name):
    values = bag.get(name) if bag else None
    if not values:
        return None
    return values[0]

def one_line(text, voice=None, speed=None, pause_ms=None):
    text = str(text or "").strip()
    if not text:
        raise ValueError("Text is empty. Send ?text= on GET /speak, or a JSON object with text.")
    return [{
        "text": text,
        "voice": voice or "af_heart",
        "speed": 1 if speed in (None, "") else speed,
        "pause_ms": 0 if pause_ms in (None, "") else pause_ms,
    }], 0

def synthesize(segments, pause_ms):
    if tts is None:
        raise RuntimeError(load_error or "Paradee 1.3 is still loading. Check the terminal.")
    pause_ms = int(pause_ms)
    if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
        raise ValueError("Provide between 1 and 200 dialogue lines.")
    if not 0 <= pause_ms <= 5000:
        raise ValueError("Pause must be between 0 and 5000 ms.")
    chunks = []
    pauses = []
    with lock:
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                raise ValueError(f"Line {i+1}: each line must be an object.")
            text = str(seg.get("text", "")).strip()
            voice = str(seg.get("voice", "af_heart"))
            speed = float(seg.get("speed", 1.0))
            seg_pause = int(seg.get("pause_ms", pause_ms))
            if not text or len(text) > 12000:
                raise ValueError(f"Line {i+1}: text must contain 1 to 12000 characters.")
            if voice not in VOICES:
                raise ValueError(f"Line {i+1}: unsupported Paradee 1.3 voice {voice!r}.")
            if not 0.5 <= speed <= 2.0:
                raise ValueError(f"Line {i+1}: speed must be between 0.5 and 2.0.")
            if not 0 <= seg_pause <= 5000:
                raise ValueError(f"Line {i+1}: pause must be between 0 and 5000 ms.")
            print(f"Synthesizing line {i+1}/{len(segments)} ({voice}, {len(text)} chars)", flush=True)
            audio = tts(text, voice=voice, speed=speed)
            chunks.append(audio)
            pauses.append(seg_pause)
            print(f"Synthesized line {i+1}/{len(segments)}", flush=True)
    wav, cues = make_wav(chunks, pauses)
    cue_header = base64.b64encode(json.dumps(cues, separators=(",", ":")).encode()).decode()
    return wav, cue_header

class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, code, data, mime="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Expose-Headers", "X-Paradee-Cues, X-Paradee-Control")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass

    def request_path(self):
        return urlparse(self.path).path

    def query(self):
        return parse_qs(urlparse(self.path).query, keep_blank_values=True)

    def read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n < 0 or n > 2_000_000:
            raise ValueError("Invalid request size.")
        return self.rfile.read(n) if n else b""

    def parse_speak(self):
        q = self.query()
        if self.command == "GET":
            return one_line(first_value(q, "text"), first_value(q, "voice"), first_value(q, "speed"), first_value(q, "pause_ms"))
        raw = self.read_body()
        if not raw:
            raise ValueError("Text is empty. Send JSON with text, or the line as plain text.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return one_line(
                first_value(form, "text"),
                first_value(form, "voice") or first_value(q, "voice"),
                first_value(form, "speed") or first_value(q, "speed"),
                first_value(form, "pause_ms") or first_value(q, "pause_ms"),
            )
        stripped = raw.lstrip()
        if ctype == "application/json" or stripped.startswith((b"{", b"[", b'"')):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON could not be read: {exc.msg}.") from exc
            if isinstance(data, str):
                return one_line(data, first_value(q, "voice"), first_value(q, "speed"), first_value(q, "pause_ms"))
            if isinstance(data, dict) and isinstance(data.get("segments"), list):
                return data["segments"], int(data.get("pause_ms", 350))
            if isinstance(data, dict):
                return one_line(data.get("text", ""), data.get("voice") or first_value(q, "voice"), data.get("speed", first_value(q, "speed")), data.get("pause_ms", first_value(q, "pause_ms")))
            raise ValueError("JSON must be an object with text, or a string.")
        return one_line(raw.decode("utf-8"), first_value(q, "voice"), first_value(q, "speed"), first_value(q, "pause_ms"))

    def serve_speech(self, segments, pause_ms, cancelable=False):
        started, _ = control_snapshot()
        try:
            wav, cues = synthesize(segments, pause_ms)
        except RuntimeError as exc:
            return self.send_bytes(503, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
        except ValueError as exc:
            return self.send_bytes(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
        except Exception as exc:
            traceback.print_exc()
            return self.send_bytes(500, str(exc).encode("utf-8", "replace"), "text/plain; charset=utf-8")
        if cancelable:
            action = claim_cancel(started)
            if action in ("stop", "skip"):
                return self.send_bytes(204, b"", extra={"X-Paradee-Control": action})
        return self.send_bytes(200, wav, "audio/wav", {"X-Paradee-Cues": cues})

    def do_OPTIONS(self):
        self.send_bytes(204, b"")

    def do_GET(self):
        path = self.request_path()
        if path in ("/", "/index.html"):
            return self.send_bytes(200, (HERE/"index.html").read_bytes(), "text/html; charset=utf-8")
        if path in ("/web/tts.js", "/web/misaki.js"):
            return self.send_bytes(200, (HERE/path.lstrip("/")).read_bytes(), "text/javascript; charset=utf-8")
        if path == "/favicon.svg":
            return self.send_bytes(200, (HERE/"favicon.svg").read_bytes(), "image/svg+xml")
        if path == "/favicon.ico":
            return self.send_bytes(200, (HERE/"favicon.ico").read_bytes(), "image/x-icon")
        if path == "/listen":
            return self.send_bytes(200, (HERE/"listen.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/channel":
            return self.send_bytes(200, json.dumps({"channel": read_channel()}).encode())
        if path == "/control":
            wait = first_value(self.query(), "wait") == "1"
            return self.send_bytes(200, json.dumps({"action": take_playback(wait)}).encode())
        if path == "/queue":
            return self.send_bytes(200, json.dumps(queue_view()).encode())
        if path == "/voices.html":
            return self.send_bytes(200, (HERE/"voices.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/health":
            return self.send_bytes(200, json.dumps({
                "ready": tts is not None, "loading": loading,
                "version": "1.3", "error": load_error,
                "voices": VOICES
            }).encode())
        if path == "/speak":
            try:
                segments, pause_ms = self.parse_speak()
            except ValueError as exc:
                return self.send_bytes(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return self.serve_speech(segments, pause_ms)
        self.send_bytes(404, b'{"error":"Not found"}')

    def do_POST(self):
        path = self.request_path()
        try:
            if path == "/channel":
                raw = self.read_body()
                if not raw:
                    name = ""
                else:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("JSON must be an object.")
                    name = data.get("channel", "")
                name = write_channel(name)
                return self.send_bytes(200, json.dumps({"channel": name}).encode())
            if path == "/control":
                data = json.loads(self.read_body() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON must be an object.")
                action = set_playback(data.get("action", ""))
                return self.send_bytes(200, json.dumps({"action": action}).encode())
            if path == "/queue":
                data = json.loads(self.read_body() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON must be an object.")
                set_queue_state(data.get("playing", ""), data.get("queue", []))
                return self.send_bytes(200, json.dumps(queue_view()).encode())
            if path == "/speak":
                segments, pause_ms = self.parse_speak()
            elif path == "/dialogue":
                data = json.loads(self.read_body() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON must be an object.")
                segments, pause_ms = data.get("segments", []), int(data.get("pause_ms", 350))
            else:
                return self.send_bytes(404, b'{"error":"Not found"}')
        except ValueError as exc:
            return self.send_bytes(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
        except json.JSONDecodeError as exc:
            return self.send_bytes(400, f"JSON could not be read: {exc.msg}.".encode("utf-8"), "text/plain; charset=utf-8")
        return self.serve_speech(segments, pause_ms, path == "/speak")

    def log_message(self, fmt, *args):
        if self.path.split("?", 1)[0] in ("/queue", "/control", "/channel", "/health"):
            return
        print("%s - %s" % (self.address_string(), fmt % args))

if __name__ == "__main__":
    print(f"Starting Paradee 1.3 Dialogue Studio at http://{HOST}:{PORT}", flush=True)
    print("Loading the v1.3 model in the background; first launch may download model/G2P files.", flush=True)
    try:
        httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"Could not listen on http://{HOST}:{PORT}: {exc}", flush=True)
        print("Close the other Paradee window, then start this one again.", flush=True)
        raise SystemExit(1)
    threading.Thread(target=initialize, daemon=True).start()
    print(f"Listening at http://{HOST}:{PORT}", flush=True)
    print(f"Other programs: http://{HOST}:{PORT}/speak?text=Hello&voice=af_heart", flush=True)
    print("Streaming TTS: https://paradee.t-tek.dev/voices.html", flush=True)
    print(f"Twitch chat: http://{HOST}:{PORT}/listen", flush=True)
    print('Chat command: !tts puck: hello', flush=True)
    httpd.serve_forever()
