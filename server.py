#!/usr/bin/env python3
"""Local-only Paradee 1.3 dialogue server. Binds exclusively to 127.0.0.1."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import io, json, threading, traceback, wave, time

HOST, PORT = "127.0.0.1", 8765
HERE = Path(__file__).resolve().parent
tts = None
load_error = None
loading = False
sample_rate = 24000
lock = threading.Lock()

# Paradee v1.3 voice IDs. Each voice determines its language/accent.
VOICES = {
    "af_heart": "English (US)", "bf_isabella": "English (UK)",
    "am_puck": "English (US)", "bm_fable": "English (UK)",
    "hf_alpha": "Hindi", "hm_omega": "Hindi",
    "ff_siwis": "French", "em_alex": "Spanish",
}

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
        print(f"Paradee 1.3 loaded ({sample_rate} Hz).")
    except Exception as exc:
        load_error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        loading = False

def make_wav(chunks, pause_ms=0):
    import numpy as np
    audio_parts = []
    silence = np.zeros(int(sample_rate * pause_ms / 1000), dtype=np.float32)
    for i, audio in enumerate(chunks):
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
        audio_parts.append(np.clip(arr, -1.0, 1.0))
        if i != len(chunks)-1 and len(silence):
            audio_parts.append(silence)
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
    return out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, code, data, mime="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send_bytes(200, (HERE/"paradee_dialogue.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/health":
            return self.send_bytes(200, json.dumps({
                "ready": tts is not None, "loading": loading,
                "version": "1.3", "error": load_error,
                "voices": VOICES
            }).encode())
        self.send_bytes(404, b'{"error":"Not found"}')

    def do_POST(self):
        if self.path != "/dialogue":
            return self.send_bytes(404, b'{"error":"Not found"}')
        if tts is None:
            return self.send_bytes(503, (load_error or "Paradee 1.3 is still loading. Check the terminal.").encode(), "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 2_000_000:
                return self.send_bytes(400, b"Invalid request size.", "text/plain")
            data = json.loads(self.rfile.read(n))
            segments = data.get("segments", [])
            pause_ms = int(data.get("pause_ms", 350))
            if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
                return self.send_bytes(400, b"Provide between 1 and 200 dialogue lines.", "text/plain")
            if not 0 <= pause_ms <= 5000:
                return self.send_bytes(400, b"Pause must be between 0 and 5000 ms.", "text/plain")
            chunks = []
            for i, seg in enumerate(segments):
                text = str(seg.get("text", "")).strip()
                voice = str(seg.get("voice", "af_heart"))
                speed = float(seg.get("speed", 1.0))
                if not text or len(text) > 12000:
                    raise ValueError(f"Line {i+1}: text must contain 1 to 12000 characters.")
                if voice not in VOICES:
                    raise ValueError(f"Line {i+1}: unsupported Paradee 1.3 voice {voice!r}.")
                if not 0.5 <= speed <= 2.0:
                    raise ValueError(f"Line {i+1}: speed must be between 0.5 and 2.0.")
                audio = tts(text, voice=voice, speed=speed)
                chunks.append(audio)
                print(f"Synthesized line {i+1}/{len(segments)} ({voice}, {len(text)} chars)")
            return self.send_bytes(200, make_wav(chunks, pause_ms), "audio/wav")
        except Exception as exc:
            traceback.print_exc()
            return self.send_bytes(500, str(exc).encode("utf-8", "replace"), "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

if __name__ == "__main__":
    print(f"Starting Paradee 1.3 Dialogue Studio at http://{HOST}:{PORT}")
    print("Loading the v1.3 model in the background; first launch may download model/G2P files.")
    threading.Thread(target=initialize, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
