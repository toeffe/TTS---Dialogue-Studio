function startChatPlayer(options) {
  options = options || {};
  const base = String(options.base || '').replace(/\/$/, '');
  const role = options.role === 'speakers' ? 'speakers' : 'obs';
  const onNeedGesture = typeof options.onNeedGesture === 'function' ? options.onNeedGesture : null;
  const statusEl = document.getElementById('status');
  const lastEl = document.getElementById('last');
  const soundBtn = document.getElementById('sound');
  const queue = [];
  let channel = '';
  let speaking = false;
  let currentAudio = null;
  let speakAbort = null;
  let stopNow = false;
  let skipNow = false;
  let playingJob = null;
  let alive = true;
  let gestureHold = false;
  let ignoreControl = 0;
  const QUEUE_LIMIT = 40;
  let clip = 0;
  let endClip = null;
  let socket = null;
  let reconnectTimer = null;
  let reportTimer = null;
  let channelTimer = null;

  const VOICE_ALIASES = {
    heart: 'af_heart', af_heart: 'af_heart',
    isabella: 'bf_isabella', bella: 'bf_isabella', bf_isabella: 'bf_isabella',
    puck: 'am_puck', am_puck: 'am_puck',
    fable: 'bm_fable', bm_fable: 'bm_fable',
    alpha: 'hf_alpha', hf_alpha: 'hf_alpha',
    omega: 'hm_omega', hm_omega: 'hm_omega',
    siwis: 'ff_siwis', ff_siwis: 'ff_siwis',
    alex: 'em_alex', em_alex: 'em_alex',
  };
  const VOICE_SAY = {
    af_heart: 'Heart', bf_isabella: 'Isabella', am_puck: 'Puck', bm_fable: 'Fable',
    hf_alpha: 'Alpha', hm_omega: 'Omega', ff_siwis: 'Siwis', em_alex: 'Alex',
  };

  function api(path) { return base + path; }
  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  function unescapeTag(value) {
    return String(value || '').replace(/\\([sn:\\r])/g, (_, c) => ({s:' ', n:' ', ':':';', '\\':'\\', r:' '}[c] || c));
  }

  function playBlob(blob) {
    const mine = ++clip;
    return new Promise(resolve => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio();
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        audio.onended = null;
        audio.onerror = null;
        if (currentAudio === audio) currentAudio = null;
        if (endClip === finish) endClip = null;
        audio.pause();
        audio.removeAttribute('src');
        try { audio.load(); } catch { /* the clip is already stopped */ }
        URL.revokeObjectURL(url);
        resolve();
      };
      currentAudio = audio;
      endClip = finish;
      audio.onended = finish;
      audio.onerror = finish;
      if (mine !== clip || stopNow || skipNow || !alive) { finish(); return; }
      audio.src = url;
      audio.play().then(() => {
        if (soundBtn) soundBtn.hidden = true;
        if (mine !== clip || stopNow || skipNow || !alive) finish();
      }).catch(() => {
        if (soundBtn) soundBtn.hidden = false;
        gestureHold = true;
        if (onNeedGesture) onNeedGesture();
        finish();
      });
    });
  }

  function haltAudio() {
    clip += 1;
    const finish = endClip;
    if (finish) finish();
  }

  function viewOf(job) {
    if (!job) return null;
    return {name: job.name || '', voice: job.voiceName || '', text: job.text || ''};
  }

  function reportQueue() {
    if (!alive) return;
    if (!channel && !playingJob && !queue.length) return;
    const body = JSON.stringify({playing: viewOf(playingJob), queue: queue.map(viewOf), role});
    fetch(api('/queue'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body
    }).catch(() => {});
  }

  function pushControl(action) {
    ignoreControl += 1;
    fetch(api('/control'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action})
    }).catch(() => { ignoreControl = Math.max(0, ignoreControl - 1); });
  }

  function requestSkip(local) {
    if (!alive) return;
    gestureHold = false;
    if (!speaking) {
      if (!queue.length) return;
      queue.shift();
      setStatus('skipped');
      reportQueue();
      return;
    }
    skipNow = true;
    if (speakAbort) speakAbort.abort();
    haltAudio();
    setStatus('skipped');
    reportQueue();
    if (local && role === 'obs') pushControl('skip');
  }

  function requestStop(local) {
    gestureHold = false;
    queue.length = 0;
    playingJob = null;
    skipNow = false;
    if (speakAbort) speakAbort.abort();
    haltAudio();
    if (speaking) stopNow = true;
    setStatus('stopped');
    reportQueue();
    if (local && role === 'obs' && alive) pushControl('stop');
  }

  async function drain() {
    if (speaking || !alive) return;
    speaking = true;
    while (queue.length && !stopNow && alive) {
      const job = queue.shift();
      playingJob = job;
      if (lastEl) lastEl.textContent = job.label;
      setStatus('playing');
      reportQueue();
      const ctrl = new AbortController();
      speakAbort = ctrl;
      try {
        const response = await fetch(api('/speak'), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text: job.text, voice: job.voice}),
          signal: ctrl.signal
        });
        if (!alive || stopNow) break;
        if (skipNow) { skipNow = false; continue; }
        if (response.status === 204) {
          if ((response.headers.get('X-Paradee-Control') || '') === 'stop') {
            stopNow = true;
            queue.length = 0;
            playingJob = null;
            break;
          }
          continue;
        }
        if (!response.ok) continue;
        const blob = await response.blob();
        if (!alive || stopNow) break;
        if (skipNow) { skipNow = false; continue; }
        await playBlob(blob);
        if (gestureHold) {
          gestureHold = false;
          if (alive && !stopNow && !skipNow) {
            queue.unshift(job);
            playingJob = null;
            speaking = false;
            reportQueue();
            return;
          }
        }
      } catch { /* aborted or failed; the next line can still play */ }
      finally { if (speakAbort === ctrl) speakAbort = null; }
      if (!alive || stopNow) break;
      if (skipNow) skipNow = false;
    }
    if (stopNow) queue.length = 0;
    stopNow = false;
    skipNow = false;
    speaking = false;
    if (!alive) return;
    if (!queue.length) {
      playingJob = null;
      reportQueue();
      setStatus(channel ? 'listening #' + channel : 'idle');
    }
    if (queue.length) drain();
  }

  function enqueue(text, voiceId, name) {
    if (!alive) return;
    if (queue.length >= QUEUE_LIMIT) queue.shift();
    const voiceName = voiceId ? (VOICE_SAY[voiceId] || voiceId) : '';
    const who = name || 'Chat';
    queue.push({
      text,
      voice: voiceId,
      name: who,
      voiceName,
      label: voiceName ? who + ' (' + voiceName + '): ' + text : who + ': ' + text
    });
    reportQueue();
    drain();
  }

  function quotedMessage(raw) {
    const text = String(raw || '').trim();
    if (!text) return '';
    const open = text[0];
    if (open === '"' || open === "'" || open === '\u201c') {
      const close = open === '\u201c' ? '\u201d' : open;
      const end = text.indexOf(close, 1);
      if (end === -1) return text.slice(1).trim();
      return text.slice(1, end).trim();
    }
    return text;
  }

  function cueOnly(message) {
    const groups = message.match(/\(([^()]*)\)/g);
    if (!groups || !groups.some(group => group.slice(1, -1).trim())) return false;
    return message.replace(/\([^()]*\)/g, '').replace(/\s+/g, '') === '';
  }

  function ttsRequest(arg) {
    let body = String(arg || '').trim();
    if (body.length >= 2) {
      const open = body[0];
      const close = open === '\u201c' ? '\u201d' : open;
      if ((open === '"' || open === "'" || open === '\u201c') && body.endsWith(close)) {
        body = body.slice(1, -1).trim();
      }
    }
    const colon = body.indexOf(':');
    const head = colon > 0 ? body.slice(0, colon).trim().toLowerCase() : '';
    const voiceId = head ? VOICE_ALIASES[head] : '';
    let message = quotedMessage(voiceId ? body.slice(colon + 1) : body).replace(/\s+/g, ' ').trim();
    if (!message) return null;
    if (!voiceId && !cueOnly(message)) return null;
    if (message.length > 180) message = message.slice(0, 177) + '...';
    return {voice: voiceId, message};
  }

  function handleLine(raw) {
    const line = raw.trim();
    if (!line || !alive) return;
    if (line.startsWith('PING')) {
      if (socket) socket.send(line.replace('PING', 'PONG'));
      return;
    }
    let rest = line;
    let tags = {};
    if (rest.startsWith('@')) {
      const splitAt = rest.indexOf(' ');
      tags = Object.fromEntries(rest.slice(1, splitAt).split(';').map(piece => {
        const eq = piece.indexOf('=');
        return eq < 0 ? [piece, ''] : [piece.slice(0, eq), piece.slice(eq + 1)];
      }));
      rest = rest.slice(splitAt + 1);
    }
    const match = rest.match(/^:([^! ]+)![^ ]* PRIVMSG #[^ ]+ :(.*)$/);
    if (!match) return;
    let text = match[2];
    if (text.charCodeAt(0) === 1 && text.endsWith('\u0001')) text = text.slice(8, -1);
    text = text.replace(/\s+/g, ' ').trim();
    const command = text.match(/^!([A-Za-z]+)(?:\s+([\s\S]*))?$/);
    if (!command) return;
    const verb = command[1].toLowerCase();
    if (verb !== 'tts') return;
    const request = ttsRequest(command[2] || '');
    if (!request) return;
    const name = unescapeTag(tags['display-name'] || match[1]);
    enqueue(request.message, request.voice, name);
  }

  function connect() {
    if (!alive) return;
    if (!channel) { setStatus('Set the channel on Streaming TTS'); return; }
    setStatus('connecting #' + channel);
    socket = new WebSocket('wss://irc-ws.chat.twitch.tv:443');
    socket.onopen = () => {
      if (!alive) { socket.close(); return; }
      socket.send('CAP REQ :twitch.tv/tags');
      socket.send('PASS SCHMOOPIIE');
      socket.send('NICK justinfan' + Math.floor(10000 + Math.random() * 89999));
      socket.send('JOIN #' + channel);
    };
    socket.onmessage = event => {
      if (!alive) return;
      for (const piece of String(event.data).split(/\r?\n/)) handleLine(piece);
    };
    socket.onclose = () => {
      if (!alive) return;
      setStatus('reconnecting #' + channel);
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 2000);
    };
    socket.onerror = () => socket.close();
  }

  const joined = new Set();
  const origHandle = handleLine;
  handleLine = raw => {
    const line = raw.trim();
    if (line.includes(' 366 ') || line.includes(' JOIN #' + channel)) {
      if (!joined.has(channel)) {
        joined.add(channel);
        setStatus('listening #' + channel);
      }
    }
    origHandle(raw);
  };

  if (soundBtn) soundBtn.onclick = () => { soundBtn.hidden = true; drain(); };
  if (!options.externalControls) {
    const skipBtn = document.getElementById('skipLine');
    const stopBtn = document.getElementById('stopLine');
    if (skipBtn) skipBtn.onclick = () => requestSkip(true);
    if (stopBtn) stopBtn.onclick = () => requestStop(true);
  }

  async function watchControl() {
    while (alive) {
      try {
        const response = await fetch(api('/control?wait=1'), {cache: 'no-store'});
        if (!alive) return;
        if (!response.ok) throw new Error('control');
        const data = await response.json();
        if (data.action === 'stop' || data.action === 'skip') {
          if (ignoreControl > 0) ignoreControl -= 1;
          else if (data.action === 'stop') requestStop(false);
          else requestSkip(false);
        } else await new Promise(resolve => setTimeout(resolve, 200));
      } catch {
        if (!alive) return;
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
  }
  if (role === 'obs') watchControl();
  reportQueue();
  reportTimer = setInterval(reportQueue, 1000);

  let refreshing = false;
  async function refreshChannel() {
    if (!alive || refreshing) return;
    refreshing = true;
    try {
      const response = await fetch(api('/channel'), {cache: 'no-store'});
      if (!alive || !response.ok) return;
      const next = String((await response.json()).channel || '').toLowerCase().replace(/[^a-z0-9_]/g, '');
      if (next === channel) return;
      channel = next;
      joined.clear();
      clearTimeout(reconnectTimer);
      if (socket) {
        socket.onclose = null;
        socket.close();
        socket = null;
      }
      connect();
      reportQueue();
    } catch { /* try again on the next check */ }
    finally { refreshing = false; }
  }
  refreshChannel();
  channelTimer = setInterval(refreshChannel, 2000);

  function stop() {
    if (!alive) return;
    alive = false;
    gestureHold = false;
    clearTimeout(reconnectTimer);
    clearInterval(reportTimer);
    clearInterval(channelTimer);
    queue.length = 0;
    playingJob = null;
    if (speakAbort) speakAbort.abort();
    haltAudio();
    stopNow = true;
    if (socket) {
      socket.onclose = null;
      try { socket.close(); } catch { /* already closed */ }
      socket = null;
    }
  }

  function resume() {
    if (!alive) return;
    gestureHold = false;
    drain();
  }

  return {stop, resume, skip: () => requestSkip(true), halt: () => requestStop(true)};
}
