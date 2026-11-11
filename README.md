# Paradee Dialogue Studio

Dialogue for English, Hindi, French, and Spanish. Two ways to run it.

## How to run it

Publish this folder as a website. `index.html` is Dialogue Studio. https://tts.t-tek.dev/voices.html lists the voices on its own. If `start.bat` is running on that PC, the same page turns into Streaming TTS.

On Windows, keep `start.bat`, `bootstrap.ps1`, `server.py`, and `index.html` in one folder, then double-click `start.bat`. The first launch needs the internet and can take several minutes. It downloads portable Python, Paradee 1.3, and the English spaCy model. No system Python and no Git are required. The browser opens at http://127.0.0.1:8765 once the server is listening. Later launches only start the server. Close the console window to stop.

Other programs on this PC can request speech while that server is running. `GET /speak?text=Hello&voice=af_heart` returns a WAV file. `POST /dialogue` accepts a full scene. `GET /health` reports when the model is ready.

## Dialogue Studio

Assign a voice to each line, then generate one WAV. Import or export the lines as JSON, or copy the AI instructions and let another model write the scene.

On the website, speech runs in the visitor’s browser. The first visit downloads the 8.5 MB Paradee model into this site’s browser cache, named `paradee-1.3`. It is not a file in Downloads. Later visits reuse it. Clearing site data downloads it again. With `start.bat` running, the same page sends speech to this PC instead.

Voices: heart, isabella, puck, fable, alpha, omega, siwis, alex.

A line can include sound cues. They play where they sit and are not spoken. `(bark)` plays one sound. `(bark&fart)` plays those sounds in that order. A MyInstants id works the same way, as in `(ketchup-flaske-99638)`. Generating a line that uses a sound adds that sound to the chat approved list. Studio has no per-message sound cap.

## Streaming TTS

This needs `start.bat`. Set the Twitch channel at http://127.0.0.1:8765/voices.html.

Twitch chat in OBS stays on the popout. Speech is a second Browser Source:

```
http://127.0.0.1:8765/listen
```

Turn on “Control audio via OBS” for that source. Ordinary chat stays quiet. A viewer speaks one line with `!tts puck: hello chat`. The name before the colon picks the voice for that line. A cue by itself needs no voice: `!tts (bark)`.

Chat plays a sound only after it is approved. A new id is saved under **Saved, not in chat** and does not play until you approve it. **Play** previews a saved sound. Clicking the id or the name copies the id. **Copy panel list** copies markdown for a Twitch About panel. Paste a MyInstants page link into **Add**, or type an id. **Remove** takes a sound out of chat and leaves the file on this PC.

**Sounds per chat message** is 0 to 8 and starts at 2. Extra cues in one message are skipped and not downloaded. `0` is speech only.

The `sounds/` folder on this PC keeps the wav files, `index.json`, `approved.txt`, and `chat-limit.txt`. Restarting the server reuses them. Loud clips are turned down so they sit under the voice.

## Credits

Speech is [Paradee 1.3](https://github.com/sahilmahendrakar/paradee) by Sahil Mahendrakar, Apache-2.0. The model is [Paradee-8M-v1.3](https://huggingface.co/sahilmahendrakar/Paradee-8M-v1.3).

Paradee is distilled from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, which builds on [StyleTTS 2](https://github.com/yl4579/StyleTTS2) and [iSTFTNet](https://arxiv.org/abs/2203.02395). Phonemes come from [misaki](https://github.com/hexgrad/misaki). On the website, English spelling uses Paradee’s `web/misaki.js`. Other languages use [eSpeak NG](https://github.com/espeak-ng/espeak-ng) (GPL-3.0).
