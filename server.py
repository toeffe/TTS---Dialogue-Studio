#!/usr/bin/env python3
"""Local-only Paradee 1.3 dialogue server. Binds exclusively to 127.0.0.1."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
import base64, hashlib, io, json, re, sys, threading, time, traceback, urllib.error, urllib.request, wave

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

# Cue level, applied when a clip is mixed. The saved wav is left as downloaded.
SOUND_PEAK = 0.4
SOUND_RMS_RATIO = 0.8
SOUND_ONLY_RMS = 0.08
SOUND_FLOOR = 10 ** (-40 / 20)
SOUNDS_DIR = HERE / "sounds"
SOUND_INDEX = SOUNDS_DIR / "index.json"
APPROVED_FILE = SOUNDS_DIR / "approved.txt"
APPROVED_LIMIT = 400
CHAT_LIMIT_FILE = SOUNDS_DIR / "chat-limit.txt"
SOUND_BYTES = 2 * 1024 * 1024
SOUND_SECONDS = 15
MAX_SOUNDS = 8
SOUND_SEARCH = "https://www.myinstants.com/api/v1/instants/"
SOUND_UA = "ParadeeDialogueStudio/1.3"
# One or two words. Three-word asides such as "(see you tomorrow)" stay spoken.
CUE_NAME = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*(?: [A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*)?")
# MyInstants slug from the instant URL, such as caching-49444 or bark-fart-38489.
SLUG_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$")
CUE_GROUP = re.compile(r"\(([^()]*)\)")
WAV_NAME = re.compile(r"^[a-z0-9-]{1,80}\.wav$")
sound_lock = threading.Lock()
sound_waiters = {}
pcm_cache = {}
decoder_note = False

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

def sound_key(name):
    return re.sub(r"\s+", " ", str(name or "").strip().lower())

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)

def strip_urls(text):
    cleaned = URL_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"\(\s*\)", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()

def cue_names(inside):
    names = []
    for piece in str(inside or "").split("&"):
        name = re.sub(r"\s+", " ", piece.strip())
        if "://" in name or name.lower().startswith("www.") or "myinstants.com/" in name.lower():
            name = cue_key(name)
        if name:
            names.append(name)
    if not 1 <= len(names) <= 4:
        return None
    if any(len(name) > 40 or CUE_NAME.fullmatch(name) is None for name in names):
        return None
    return names

def split_cues(text):
    parts = []
    cursor = 0
    used = 0
    for match in CUE_GROUP.finditer(text):
        names = cue_names(match.group(1))
        if not names or used + len(names) > MAX_SOUNDS:
            continue
        used += len(names)
        parts.append(("text", text[cursor:match.start()]))
        parts.append(("sounds", names))
        cursor = match.end()
    parts.append(("text", text[cursor:]))
    return parts

def active_rms(samples):
    import numpy as np
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return 0.0
    loud = arr[np.abs(arr) >= SOUND_FLOOR]
    if loud.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(loud.astype(np.float64)))))

def tame_clip(samples, speech_rms):
    import numpy as np
    clip = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
    if clip.size == 0:
        return clip
    target = SOUND_ONLY_RMS if speech_rms <= 0 else speech_rms * SOUND_RMS_RATIO
    for _ in range(4):
        peak = float(np.max(np.abs(clip)))
        if not np.isfinite(peak) or peak <= 0:
            return np.zeros_like(clip)
        rms = active_rms(clip)
        peak_gain = SOUND_PEAK / peak if peak > SOUND_PEAK else 1.0
        rms_gain = target / rms if rms > target and target > 0 else 1.0
        gain = min(1.0, peak_gain, rms_gain)
        if gain >= 0.999:
            break
        clip *= np.float32(gain)
    np.clip(clip, -SOUND_PEAK, SOUND_PEAK, out=clip)
    return clip

def read_index():
    try:
        data = json.loads(SOUND_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def write_index(index):
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(index, indent=2, ensure_ascii=False).encode("utf-8")
    tmp = SOUNDS_DIR / "index.json.tmp"
    tmp.write_bytes(blob)
    tmp.replace(SOUND_INDEX)

def classify_sound(key):
    entry = read_index().get(key)
    if not isinstance(entry, dict):
        return "absent", None, ""
    if entry.get("miss"):
        return "miss", None, ""
    file_name = str(entry.get("file") or "")
    if WAV_NAME.fullmatch(file_name) is None:
        return "absent", None, ""
    path = SOUNDS_DIR / file_name
    if not path.is_file():
        return "absent", None, ""
    label = str(entry.get("name") or key).strip() or key
    return "hit", path, label

def remember_sound(key, entry):
    with sound_lock:
        index = read_index()
        index[key] = entry
        write_index(index)

def read_wav_float(path):
    import numpy as np
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("cached sound is not mono 16-bit")
        rate = wav.getframerate() or sample_rate
        frames = wav.readframes(wav.getnframes())
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if rate != sample_rate:
        samples = resample(samples, rate, sample_rate)
    return samples

def resample(samples, src_rate, dst_rate):
    import numpy as np
    if src_rate == dst_rate or len(samples) == 0:
        return np.asarray(samples, dtype=np.float32)
    dst_len = max(1, int(round(len(samples) * float(dst_rate) / float(src_rate))))
    src_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    return np.interp(dst_x, src_x, np.asarray(samples, dtype=np.float32)).astype(np.float32)

def write_wav_file(path, samples):
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)

def pcm_for(path):
    token = str(path)
    with sound_lock:
        cached = pcm_cache.get(token)
    if cached is not None:
        return cached
    data = read_wav_float(path)
    with sound_lock:
        pcm_cache[token] = data
    return data

def allowed_media(url):
    parsed = urlparse(str(url or ""))
    path = parsed.path or ""
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.myinstants.com"
        and path.startswith("/media/")
        and path.lower().endswith(".mp3")
    )

def fetch_bytes(url, limit, accept):
    request = urllib.request.Request(url, headers={"User-Agent": SOUND_UA, "Accept": accept})
    with urllib.request.urlopen(request, timeout=20) as response:
        final = response.geturl()
        chunks = []
        total = 0
        while True:
            block = response.read(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ValueError("The download is larger than the limit.")
            chunks.append(block)
        return b"".join(chunks), final

def fetch_instant(slug):
    body, final = fetch_bytes(SOUND_SEARCH + quote(slug) + "/?format=json", 1_000_000, "application/json")
    host = urlparse(final).netloc
    if host not in ("www.myinstants.com", "myinstants.com"):
        raise ValueError("MyInstants search left the expected site.")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("MyInstants returned an unexpected sound.")
    got = str(data.get("slug") or "").strip().lower()
    if got != slug or not data.get("sound"):
        return None
    return data

def search_instants(key):
    query = urlencode({"name": key, "page": "1", "format": "json"})
    body, final = fetch_bytes(SOUND_SEARCH + "?" + query, 1_000_000, "application/json")
    host = urlparse(final).netloc
    if host not in ("www.myinstants.com", "myinstants.com"):
        raise ValueError("MyInstants search left the expected site.")
    data = json.loads(body.decode("utf-8"))
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ValueError("MyInstants returned an unexpected list.")
    return results

def pick_instant(results, key):
    slug_key = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    exact = None
    slug_hit = None
    first = None
    for item in results:
        if not isinstance(item, dict) or not item.get("sound"):
            continue
        if first is None:
            first = item
        if exact is None and sound_key(item.get("name") or "") == key:
            exact = item
        slug = str(item.get("slug") or "").strip().lower()
        if slug_hit is None and slug and slug == slug_key:
            slug_hit = item
    return exact or slug_hit or first

def wav_name(slug, source):
    clean = re.sub(r"[^a-z0-9-]+", "-", str(slug or "").lower()).strip("-")[:80]
    if not clean:
        clean = "clip-" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return clean + ".wav"

def decode_mp3(payload):
    global decoder_note
    try:
        import miniaudio
    except ImportError:
        if not decoder_note:
            decoder_note = True
            print("Sound cues need miniaudio. Start start.bat again so it can install.", flush=True)
        return None
    import numpy as np
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SOUNDS_DIR / f".decode-{threading.get_ident()}.mp3"
    tmp.write_bytes(payload)
    try:
        decoded = miniaudio.decode_file(
            str(tmp),
            output_format=miniaudio.SampleFormat.FLOAT32,
            nchannels=1,
            sample_rate=sample_rate,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    samples = np.array(decoded.samples, dtype=np.float32, copy=True)
    channels = int(getattr(decoded, "nchannels", 1) or 1)
    if channels > 1 and samples.size % channels == 0:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)
    rate = int(getattr(decoded, "sample_rate", sample_rate) or sample_rate)
    if rate != sample_rate:
        samples = resample(samples, rate, sample_rate)
    return samples

def download_sound(key):
    try:
        if SLUG_ID.fullmatch(key):
            chosen = fetch_instant(key)
        else:
            chosen = pick_instant(search_instants(key), key)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            remember_sound(key, {"miss": True})
            print(f"Sound {key!r} not found", flush=True)
        elif exc.code == 429:
            print(f"Sound {key!r} was rate limited. It will be tried again later.", flush=True)
        else:
            print(f"Sound {key!r} search failed ({exc.code}).", flush=True)
        return None
    except Exception as exc:
        print(f"Sound {key!r} search failed ({exc}).", flush=True)
        return None
    if chosen is None:
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} not found", flush=True)
        return None
    source = str(chosen.get("sound") or "")
    label = str(chosen.get("name") or key).strip() or key
    if not allowed_media(source):
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} not found", flush=True)
        return None
    file_name = wav_name(chosen.get("slug") or "", source)
    if WAV_NAME.fullmatch(file_name) is None:
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} not found", flush=True)
        return None
    path = SOUNDS_DIR / file_name
    if path.is_file() and path.stat().st_size > 44:
        remember_sound(key, {"name": label, "slug": str(chosen.get("slug") or ""), "file": file_name, "source": source})
        print(f"Sound {key!r} from cache ({label})", flush=True)
        try:
            return pcm_for(path)
        except (OSError, wave.Error, ValueError):
            print(f"Sound {key!r} could not be read", flush=True)
            return None
    try:
        payload, final = fetch_bytes(source, SOUND_BYTES, "audio/mpeg, */*")
    except urllib.error.HTTPError as exc:
        print(f"Sound {key!r} download failed ({exc.code}).", flush=True)
        return None
    except ValueError:
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} is larger than 2 MB.", flush=True)
        return None
    except Exception as exc:
        print(f"Sound {key!r} download failed ({exc}).", flush=True)
        return None
    if not allowed_media(final):
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} download left MyInstants.", flush=True)
        return None
    try:
        samples = decode_mp3(payload)
    except Exception as exc:
        print(f"Sound {key!r} could not be read ({exc}).", flush=True)
        remember_sound(key, {"miss": True})
        return None
    if samples is None:
        return None
    if len(samples) == 0:
        remember_sound(key, {"miss": True})
        print(f"Sound {key!r} could not be read", flush=True)
        return None
    trimmed = samples.size > sample_rate * SOUND_SECONDS
    if trimmed:
        samples = samples[:sample_rate * SOUND_SECONDS]
    part = path.with_name(f"{path.name}.{threading.get_ident()}.part")
    try:
        write_wav_file(part, samples)
        part.replace(path)
    except OSError as exc:
        print(f"Sound {key!r} could not be saved ({exc}).", flush=True)
        return None
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    remember_sound(key, {"name": label, "slug": str(chosen.get("slug") or ""), "file": file_name, "source": source})
    with sound_lock:
        pcm_cache[str(path)] = samples
    print(f"Sound {key!r} downloaded ({label})", flush=True)
    if trimmed:
        print(f"Sound {key!r} trimmed to 15 seconds", flush=True)
    return samples

def cue_key(raw):
    text = str(raw or "").strip().strip("<>\"'")
    if "://" in text or text.lower().startswith("www.") or "myinstants.com/" in text.lower():
        if "://" not in text:
            text = "https://" + text
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host != "myinstants.com":
            return ""
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        text = parts[-1] if parts else ""
    elif len(text) >= 2 and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    names = cue_names(text)
    if not names or len(names) != 1:
        return ""
    return sound_key(names[0])

def read_approved():
    try:
        text = APPROVED_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    keys = []
    seen = set()
    for line in text.splitlines():
        key = sound_key(line)
        if not key or key in seen or cue_names(key) is None:
            continue
        seen.add(key)
        keys.append(key)
    return keys

def write_approved(keys):
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    blob = "".join(key + "\n" for key in keys).encode("utf-8")
    tmp = APPROVED_FILE.with_suffix(".txt.tmp")
    tmp.write_bytes(blob)
    tmp.replace(APPROVED_FILE)

def approve_sound(key):
    with sound_lock:
        keys = read_approved()
        if key in keys:
            return True
        if len(keys) >= APPROVED_LIMIT:
            print("Approved sound list is full (400).", flush=True)
            return False
        keys.append(key)
        write_approved(keys)
    print(f"Sound {key!r} approved.", flush=True)
    return True

def unapprove_sound(key):
    with sound_lock:
        keys = [item for item in read_approved() if item != key]
        write_approved(keys)
    print(f"Sound {key!r} removed from the approved list.", flush=True)

def sound_catalog():
    with sound_lock:
        index = read_index()
        approved = read_approved()
    saved = []
    for key, entry in index.items():
        if not isinstance(entry, dict) or entry.get("miss"):
            continue
        saved.append({
            "key": key,
            "name": str(entry.get("name") or key),
            "slug": str(entry.get("slug") or ""),
            "approved": key in approved,
        })
    saved.sort(key=lambda item: item["key"])
    return {"approved": approved, "saved": saved, "chatLimit": read_chat_limit()}

def read_chat_limit():
    try:
        value = int(CHAT_LIMIT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 2
    return max(0, min(MAX_SOUNDS, value))

def write_chat_limit(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = -1
    if not 0 <= number <= MAX_SOUNDS:
        raise ValueError(f"Use 0 to {MAX_SOUNDS} sounds per chat message.")
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHAT_LIMIT_FILE.with_suffix(".txt.tmp")
    tmp.write_text(str(number) + "\n", encoding="utf-8")
    tmp.replace(CHAT_LIMIT_FILE)
    print(f"Chat sound limit is {number}.", flush=True)
    return number

def limit_chat_sounds(lines, limit):
    for line in lines:
        kept = 0
        parts = []
        dropped = 0
        for kind, value in line["parts"]:
            if kind != "sounds":
                parts.append((kind, value))
                continue
            names = []
            for name in value:
                if kept >= limit:
                    dropped += 1
                    continue
                names.append(name)
                kept += 1
            if names:
                parts.append(("sounds", names))
        if dropped:
            print(f"Chat sound limit is {limit}. Skipped {dropped}.", flush=True)
        line["parts"] = parts

def wav_bytes(samples):
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()

def preview_wav(raw):
    key = cue_key(raw)
    if not key:
        return None
    with sound_lock:
        status, path, _label = classify_sound(key)
    if status != "hit":
        return None
    try:
        samples = pcm_for(path)
    except (OSError, wave.Error, ValueError):
        return None
    return wav_bytes(tame_clip(samples, 0.0))

def load_sound(name):
    key = sound_key(name)
    if not key:
        return None
    with sound_lock:
        status, path, label = classify_sound(key)
        event = None
        owner = False
        if status == "absent":
            event = sound_waiters.get(key)
            if event is None:
                event = threading.Event()
                sound_waiters[key] = event
                owner = True
    if status == "miss":
        print(f"Sound {key!r} not found", flush=True)
        return None
    if status == "hit":
        try:
            audio = pcm_for(path)
        except (OSError, wave.Error, ValueError):
            print(f"Sound {key!r} could not be read", flush=True)
            return None
        print(f"Sound {key!r} from cache ({label})", flush=True)
        return audio
    if not owner:
        event.wait(45)
        with sound_lock:
            status, path, label = classify_sound(key)
        if status != "hit":
            return None
        try:
            audio = pcm_for(path)
        except (OSError, wave.Error, ValueError):
            return None
        print(f"Sound {key!r} from cache ({label})", flush=True)
        return audio
    try:
        return download_sound(key)
    finally:
        with sound_lock:
            sound_waiters.pop(key, None)
        event.set()

def prepare_line(index, seg, pause_ms):
    if not isinstance(seg, dict):
        raise ValueError(f"Line {index}: each line must be an object.")
    text = str(seg.get("text", "")).strip()
    voice = str(seg.get("voice") or "").strip()
    speed = float(seg.get("speed", 1.0))
    seg_pause = int(seg.get("pause_ms", pause_ms))
    if not text or len(text) > 12000:
        raise ValueError(f"Line {index}: text must contain 1 to 12000 characters.")
    parts = split_cues(text)
    spoken = strip_urls(" ".join(piece for kind, piece in parts if kind == "text"))
    if voice not in VOICES:
        if spoken:
            raise ValueError(f"Line {index}: unsupported Paradee 1.3 voice {voice!r}.")
        voice = ""
    if not 0.5 <= speed <= 2.0:
        raise ValueError(f"Line {index}: speed must be between 0.5 and 2.0.")
    if not 0 <= seg_pause <= 5000:
        raise ValueError(f"Line {index}: pause must be between 0 and 5000 ms.")
    return {"text": text, "voice": voice, "speed": speed, "pause": seg_pause, "parts": parts}

def render_line(index, total, line, clips):
    import numpy as np
    who = line["voice"] or "sound"
    print(f"Synthesizing line {index}/{total} ({who}, {len(line['text'])} chars)", flush=True)
    speech_parts = []
    sequence = []
    for kind, value in line["parts"]:
        if kind == "text":
            spoken = strip_urls(value)
            if not spoken:
                continue
            audio = np.asarray(tts(spoken, voice=line["voice"], speed=line["speed"]), dtype=np.float32).reshape(-1)
            if audio.size == 0:
                continue
            audio = np.clip(audio, -1.0, 1.0)
            speech_parts.append(audio)
            sequence.append(("speech", audio))
            continue
        for name in value:
            sequence.append(("sound", clips.get(sound_key(name))))
    sound_ready = any(kind == "sound" and audio is not None and len(audio) for kind, audio in sequence)
    if not speech_parts and not sound_ready:
        if line.get("held"):
            print(f"Skipped line {index}/{total}. The sound is not approved.", flush=True)
            return None
        raise ValueError(f"Line {index}: no speech and no saved sound.")
    speech_rms = active_rms(np.concatenate(speech_parts)) if speech_parts else 0.0
    pieces = []
    for kind, audio in sequence:
        if kind == "speech":
            pieces.append(audio)
        elif audio is not None and len(audio):
            pieces.append(tame_clip(audio, speech_rms))
    if not pieces:
        if line.get("held"):
            print(f"Skipped line {index}/{total}. The sound is not approved.", flush=True)
            return None
        raise ValueError(f"Line {index}: no speech and no saved sound.")
    print(f"Synthesized line {index}/{total}", flush=True)
    return np.concatenate(pieces)

def collect_clips(lines, approved_only):
    allowed = set(read_approved()) if approved_only else None
    resolved = []
    for line in lines:
        clips = {}
        for kind, value in line["parts"]:
            if kind != "sounds":
                continue
            for name in value:
                key = sound_key(name)
                if key in clips:
                    continue
                if allowed is not None and key not in allowed:
                    saved = load_sound(name) is not None
                    clips[key] = None
                    line["held"] = True
                    if saved:
                        print(f"Sound {key!r} is not approved. Saved for review.", flush=True)
                    else:
                        print(f"Sound {key!r} is not approved.", flush=True)
                    continue
                audio = load_sound(name)
                clips[key] = audio
                if audio is not None and allowed is None:
                    approve_sound(key)
        resolved.append(clips)
    return resolved

def synthesize(segments, pause_ms, approved_only=False):
    if tts is None:
        raise RuntimeError(load_error or "Paradee 1.3 is still loading. Check the terminal.")
    pause_ms = int(pause_ms)
    if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
        raise ValueError("Provide between 1 and 200 dialogue lines.")
    if not 0 <= pause_ms <= 5000:
        raise ValueError("Pause must be between 0 and 5000 ms.")
    lines = [prepare_line(i + 1, seg, pause_ms) for i, seg in enumerate(segments)]
    if approved_only:
        limit_chat_sounds(lines, read_chat_limit())
    resolved = collect_clips(lines, approved_only)
    chunks = []
    pauses = []
    with lock:
        for index, (line, clips) in enumerate(zip(lines, resolved), start=1):
            chunk = render_line(index, len(lines), line, clips)
            if chunk is None:
                continue
            chunks.append(chunk)
            pauses.append(line["pause"])
    if not chunks:
        return None
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

    def serve_speech(self, segments, pause_ms, cancelable=False, approved_only=False):
        started, _ = control_snapshot()
        try:
            spoken = synthesize(segments, pause_ms, approved_only)
            if spoken is None:
                return self.send_bytes(204, b"")
            wav, cues = spoken
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
        if path == "/sounds/preview":
            data = preview_wav(first_value(self.query(), "key"))
            if data is None:
                return self.send_bytes(404, b"That sound is not saved.", "text/plain; charset=utf-8")
            return self.send_bytes(200, data, "audio/wav")
        if path == "/sounds":
            return self.send_bytes(200, json.dumps(sound_catalog()).encode())
        if path == "/speak":
            try:
                segments, pause_ms = self.parse_speak()
            except ValueError as exc:
                return self.send_bytes(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return self.serve_speech(segments, pause_ms, approved_only=True)
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
            if path == "/sounds":
                data = json.loads(self.read_body() or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON must be an object.")
                if "chatLimit" in data:
                    write_chat_limit(data.get("chatLimit"))
                    return self.send_bytes(200, json.dumps(sound_catalog()).encode())
                if "remove" in data:
                    key = cue_key(data.get("remove"))
                    if not key:
                        raise ValueError("That is not a sound name or id.")
                    unapprove_sound(key)
                    return self.send_bytes(200, json.dumps(sound_catalog()).encode())
                key = cue_key(data.get("add"))
                if not key:
                    raise ValueError("Paste a MyInstants link, or type an id such as caching-49444.")
                if load_sound(key) is None:
                    raise ValueError(f"Sound {key} was not found.")
                if not approve_sound(key):
                    raise ValueError("The approved list is full (400). Remove one, then add this.")
                return self.send_bytes(200, json.dumps(sound_catalog()).encode())
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
        chat = path == "/speak"
        return self.serve_speech(segments, pause_ms, chat, chat)

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
