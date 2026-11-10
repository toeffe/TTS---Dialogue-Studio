# Paradee Dialogue Studio

Dialogue studio for English, Hindi, French, and Spanish. Two ways to run it.

## Website

Publish this folder with GitHub Pages. The site root is `index.html`. Speech runs in the visitor’s browser. Nothing is installed.

The first visit needs the internet. The page downloads the 8.5 MB Paradee model into this site’s browser cache, named `paradee-1.3`. It is not a file in Downloads. In Chrome that cache is under DevTools, Application, Cache Storage. Later visits reuse it. Clearing site data downloads it again. The page says when it is downloading, when it is using the saved copy, and which line it is speaking.

## Download (Windows x64)

This path runs Paradee on the PC. Keep `start.bat`, `bootstrap.ps1`, `server.py`, and `paradee_dialogue.html` in one folder, then double-click `start.bat`.

The first launch needs the internet and can take several minutes. No system Python and no Git are required. The script downloads portable Python 3.10.11 into `.venv`, then installs Paradee 1.3 and the English spaCy model `en_core_web_sm`. The browser opens at http://127.0.0.1:8765 once the server is listening. The speech model downloads the first time the server starts.

If setup stops after Python is in `.venv`, double-click `start.bat` again. It continues from where it left off. Later launches only start the server. Close the console window to stop.

Manual start, after setup:

```
.venv\python.exe server.py
```

If `.venv` is a normal virtual environment, use `.venv\Scripts\python.exe server.py` instead.

## Credits

Speech is [Paradee 1.3](https://github.com/sahilmahendrakar/paradee) by Sahil Mahendrakar, Apache-2.0. The model is [Paradee-8M-v1.3](https://huggingface.co/sahilmahendrakar/Paradee-8M-v1.3).

Paradee is distilled from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, which builds on [StyleTTS 2](https://github.com/yl4579/StyleTTS2) and [iSTFTNet](https://arxiv.org/abs/2203.02395). Phonemes come from [misaki](https://github.com/hexgrad/misaki). On the website, English spelling uses Paradee’s `web/misaki.js`. Other languages use [eSpeak NG](https://github.com/espeak-ng/espeak-ng) (GPL-3.0).
