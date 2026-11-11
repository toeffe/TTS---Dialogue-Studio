# Paradee Dialogue Studio

A text-to-speech tool for writing dialogue scenes in English, Hindi, French, and Spanish.

It works two ways:
- **Dialogue Studio** — write a scene, assign voices, generate audio. Runs entirely in your browser.
- **Streaming TTS** — reads Twitch chat out loud in OBS. Requires running a local server.

---

## Quick start

**Just want to write dialogue?**
Open `index.html`, or visit https://tts.t-tek.dev/index.html — no install needed. Speech runs in your browser. Skip to [Dialogue Studio](#dialogue-studio).

**Want Twitch chat to talk in your stream?**
You need the local server running. Skip to [Running the local server](#running-the-local-server).

---

## Running the local server

The local server is required for Streaming TTS, and it also upgrades Dialogue Studio to use your PC instead of the browser.

**Setup (Windows only):**

1. Clone the project:
   ```
   git clone https://github.com/toeffe/paradee-Dialogue-Studio.git
   ```
   No Git installed? Download and unzip https://github.com/toeffe/paradee-Dialogue-Studio/archive/refs/heads/main.zip
2. Open that folder and double-click `start.bat`.

**First launch:** needs an internet connection and can take several minutes. It automatically downloads a portable Python, the Paradee 1.3 model, and the English spaCy model — you don't need Python already installed.

**Every launch after that:** just starts the server, no downloads.

Once it's ready, your browser opens automatically at:
```
http://127.0.0.1:8765
```

To stop the server, close the console window.

### Using the server from other apps

While the server is running, any program on your PC can request speech from it:

| Request | What it does |
|---|---|
| `GET /speak?text=Hello&voice=af_heart` | Returns a WAV file of that text |
| `POST /dialogue` | Generates audio for a full scene |
| `GET /health` | Tells you whether the model has finished loading |

---

## Dialogue Studio

Write a scene line by line, assign a voice to each line, then generate a single WAV file for the whole thing.

- **Import/export**: save or load your scene as a JSON file.
- **AI-assisted writing**: copy the built-in AI instructions and paste them into another AI model to have it write a scene for you in the correct format.

**Available voices:** heart, isabella, puck, fable, alpha, omega, siwis, alex.

### Where the speech is generated

- **Without the server running:** speech is generated in your browser. On your first visit, it downloads an 8.5 MB model (`paradee-1.3`) into your browser's cache — this is *not* a file that appears in your Downloads folder. Later visits reuse the cached copy automatically. If you clear your browser's site data, it will download again.
- **With `start.bat` running:** the same page automatically switches to using your PC's server instead.

### Sound effects in dialogue

You can drop sound cues into a line of dialogue. They play at that point in the line but are not read aloud as text.

| Example | Effect |
|---|---|
| `(bark-243987)` | Plays one sound |
| `(bark&fart-158744)` | Plays both sounds, in that order |
| `(ketchup-flaske-99638)` | Plays a sound by its MyInstants ID |

Generating a line that uses a sound automatically adds that sound to the chat-approved list (see below). There's no limit on how many sound cues one line can contain in Dialogue Studio.

---

## Streaming TTS

Reads Twitch chat messages aloud in your stream. **Requires the local server (`start.bat`) to be running.**

### Setup

1. Go to `http://127.0.0.1:8765/voices.html` and set your Twitch channel name.
2. Add a Browser Source in OBS pointing to:
   ```
   http://127.0.0.1:8765/listen
   ```
3. On that Browser Source, enable **"Control audio via OBS."** Leave that source’s audio monitor off.
4. On the Streaming TTS page, turn on **Play through speakers** to hear chat on this PC. Chat still plays when OBS is closed. With OBS open, the same lines also come out of the speakers. If the monitor is on, you hear each line twice.

### How viewers use it

Chat is silent by default. A viewer can trigger speech with:
```
!tts puck: hello chat
```
The name before the colon (`puck`) chooses the voice for that line.

### Sound effects in chat

Unlike Dialogue Studio, chat sounds need to be **approved** before they'll play:

- When a new sound ID shows up in chat, it's saved under **"Saved, not in chat"** — it won't play yet.
- **Play** previews a saved sound before you approve it.
- Click a sound's ID or name to copy it.
- **Add** a new sound by pasting a MyInstants page link, or typing an ID directly.
- **Remove** takes a sound out of the approved list — it stays saved on your PC but stops playing in chat.

**Sounds per chat message** is a setting from 0–8 (default: 2). Any extra sound cues beyond that limit in a single message are ignored and never downloaded. Set it to `0` for speech-only, no sounds.

### Where everything is stored

All of this lives in the `sounds/` folder on your PC:

| File | Contents |
|---|---|
| `*.wav` | The actual sound clips |
| `index.json` | Sound metadata |
| `approved.txt` | Which sounds are approved to play in chat |
| `chat-limit.txt` | Your sounds-per-message setting |

Restarting the server reuses everything already in this folder. Loud clips are automatically turned down so they don't overpower the voice audio.



## Credits

Speech is [Paradee 1.3](https://github.com/sahilmahendrakar/paradee) by Sahil Mahendrakar, Apache-2.0. The model is [Paradee-8M-v1.3](https://huggingface.co/sahilmahendrakar/Paradee-8M-v1.3).
Paradee is distilled from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, which builds on [StyleTTS 2](https://github.com/yl4579/StyleTTS2) and [iSTFTNet](https://arxiv.org/abs/2203.02395). Phonemes come from [misaki](https://github.com/hexgrad/misaki). On the website, English spelling uses Paradee’s `web/misaki.js`. Other languages use [eSpeak NG](https://github.com/espeak-ng/espeak-ng) (GPL-3.0).