// Paradee 1.3 in the browser: eSpeak phonemes, then the int8 ONNX model.
import { createEspeak } from 'https://cdn.jsdelivr.net/npm/@spelling-creator/spellophone@0.2.1/dist/browser.js';
import * as ort from 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.21.0/dist/ort.wasm.min.mjs';
import { toMisaki } from './misaki.js';

const REPO = 'https://huggingface.co/sahilmahendrakar/Paradee-8M-v1.3/resolve/v1.3';
const CONFIG_URL = `${REPO}/config.json`;
const MODEL_URL = `${REPO}/onnx/paradee_int8.onnx`;
const CACHE = 'paradee-1.3';
const MAX_PHONEMES = 510;

const LANG = {
  af_heart: 'en-us', am_puck: 'en-us',
  bf_isabella: 'en-gb', bm_fable: 'en-gb',
  hf_alpha: 'hi', hm_omega: 'hi',
  ff_siwis: 'fr-fr', em_alex: 'es',
};

// Misaki EspeakG2P renames, with '^' standing for eSpeak's tie mark.
const E2M_OTHER = [
  ['a^ɪ', 'I'], ['a^ʊ', 'W'],
  ['d^z', 'ʣ'], ['d^ʒ', 'ʤ'],
  ['e^ɪ', 'A'],
  ['o^ʊ', 'O'], ['ə^ʊ', 'Q'],
  ['s^s', 'S'],
  ['t^s', 'ʦ'], ['t^ʃ', 'ʧ'],
  ['ɔ^ɪ', 'Y'],
];

ort.env.wasm.numThreads = 1;
ort.env.wasm.proxy = true;
ort.env.wasm.wasmPaths = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.21.0/dist/';

async function cached(url, onBytes) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(url);
  if (hit) return { buffer: await hit.arrayBuffer(), fromCache: true };
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Download failed (${response.status})`);
  const total = Number(response.headers.get('Content-Length')) || 0;
  const reader = response.body && response.body.getReader();
  if (!reader) {
    const buffer = await response.arrayBuffer();
    await cache.put(url, new Response(buffer.slice(0)));
    return { buffer, fromCache: false };
  }
  const chunks = [];
  let received = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    if (onBytes) onBytes(received, total);
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  await cache.put(url, new Response(bytes.slice(0), {
    headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/octet-stream' },
  }));
  return { buffer: bytes.buffer, fromCache: false };
}

function stripUrls(text) {
  return String(text || '')
    .replace(/(?:https?:\/\/|www\.)[^\s<>"']+/gi, ' ')
    .replace(/\(\s*\)/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function sentences(text) {
  return text.trim().split(/(?<=[.!?…])\s+|(?<=[।॥])\s*|\n+/u).map(s => s.trim()).filter(Boolean);
}

function chunks(phonemes) {
  const out = [];
  let rest = phonemes.trim();
  while (rest.length > MAX_PHONEMES) {
    let cut = rest.lastIndexOf(' ', MAX_PHONEMES);
    if (cut <= 0) cut = MAX_PHONEMES;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut).trimStart();
  }
  if (rest) out.push(rest);
  return out;
}

function prepareText(text, english) {
  if (english) return text;
  return text.replace(/«/g, '\u201c').replace(/»/g, '\u201d').replace(/\(/g, '«').replace(/\)/g, '»');
}

function mapOther(ps) {
  ps = ps.replace(/[\u0361\u035C\u203F\u2040\u035D]/g, '^');
  for (const [from, to] of E2M_OTHER) ps = ps.split(from).join(to);
  return ps.replace(/\^/g, '').replace(/-/g, '').replace(/«/g, '(').replace(/»/g, ')');
}

function phonemize(espeak, text, voice) {
  const lang = LANG[voice];
  if (!lang) throw new Error(`Unsupported voice ${voice}`);
  const english = lang.startsWith('en');
  const raw = espeak.phonemes(prepareText(text, english), { voice: lang, ipa: true, tie: !english });
  const ps = raw.replace(/\s*\n+\s*/g, ' ').trim();
  return english ? toMisaki(ps, lang === 'en-gb') : mapOther(ps);
}

function tokenIds(phonemes, vocab) {
  const ids = [0];
  for (const ch of phonemes) {
    const id = vocab[ch];
    if (id === undefined) continue;
    ids.push(id);
    if (ids.length === MAX_PHONEMES + 1) break;
  }
  ids.push(0);
  return ids;
}

async function runChunk(session, ids, speed, voiceIndex) {
  const out = await session.run({
    input_ids: new ort.Tensor('int64', BigInt64Array.from(ids, n => BigInt(n)), [1, ids.length]),
    speed: new ort.Tensor('float32', Float32Array.of(speed), [1]),
    voice: new ort.Tensor('int64', BigInt64Array.of(BigInt(voiceIndex)), [1]),
  });
  const waveform = out.waveform || out[session.outputNames[0]];
  return waveform.data;
}

function concat(parts) {
  let total = 0;
  for (const part of parts) total += part.length;
  const joined = new Float32Array(total);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}

function joinLines(lines, pauses) {
  let total = 0;
  for (let i = 0; i < lines.length; i++) {
    total += lines[i].length;
    if (i !== lines.length - 1) total += pauses[i];
  }
  const joined = new Float32Array(total);
  let offset = 0;
  for (let i = 0; i < lines.length; i++) {
    joined.set(lines[i], offset);
    offset += lines[i].length;
    if (i !== lines.length - 1) offset += pauses[i];
  }
  return joined;
}

export function encodeWav(samples, sampleRate) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(bytes);
  const write = (offset, text) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  write(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, 'WAVE');
  write(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const sample = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
    offset += 2;
  }
  return new Blob([bytes], { type: 'audio/wav' });
}

export async function loadEngine(onStatus) {
  const report = (text, progress) => onStatus({ text, progress });
  report('Loading voices…');
  const espeakPromise = createEspeak({ cdn: 'jsdelivr', languages: ['en', 'hi', 'fr', 'es'] });
  const configPromise = cached(CONFIG_URL);
  const espeak = await espeakPromise;
  const hadModel = Boolean(await (await caches.open(CACHE)).match(MODEL_URL));
  report(
    hadModel
      ? 'Loading the speech model already saved in this browser…'
      : 'Downloading the speech model, about 8.5 MB. It is saved in this browser, not as a file.',
    hadModel ? undefined : 0,
  );
  const model = await cached(MODEL_URL, (got, total) => {
    if (!total) return;
    const pct = Math.round(got / total * 100);
    report('Downloading the speech model. It is saved in this browser, not as a file.', pct);
  });
  report('Starting the model…');
  const configBytes = await configPromise;
  const config = JSON.parse(new TextDecoder().decode(configBytes.buffer));
  const session = await ort.InferenceSession.create(model.buffer, { executionProviders: ['wasm'] });
  return {
    espeak, session, config,
    sampleRate: config.sample_rate || 24000,
    fromCache: model.fromCache,
  };
}

function stopError() {
  const error = new Error('Stopped.');
  error.cancelled = true;
  return error;
}

export async function synthesize(engine, segments, onProgress, signal) {
  const { espeak, session, config, sampleRate } = engine;
  const lines = [];
  for (let i = 0; i < segments.length; i++) {
    const segment = segments[i];
    const voice = segment.voice in LANG ? segment.voice : 'af_heart';
    const speed = Math.max(0.5, Math.min(2, Number(segment.speed) || 1));
    const voiceIndex = Math.max(0, config.voices.indexOf(voice));
    const jobs = [];
    for (const sentence of sentences(stripUrls(segment.text))) {
      const phonemes = phonemize(espeak, sentence, voice);
      for (const piece of chunks(phonemes)) jobs.push(piece);
    }
    const steps = Math.max(1, jobs.length);
    const pieces = [];
    for (let step = 0; step < steps; step++) {
      if (signal && signal.cancelled) throw stopError();
      if (onProgress) onProgress(i + step / steps, segments.length);
      await new Promise(resolve => setTimeout(resolve, 30));
      if (signal && signal.cancelled) throw stopError();
      if (jobs[step]) pieces.push(await runChunk(session, tokenIds(jobs[step], config.vocab), speed, voiceIndex));
    }
    lines.push(pieces.length ? concat(pieces) : new Float32Array(0));
  }
  if (signal && signal.cancelled) throw stopError();
  if (onProgress) onProgress(segments.length, segments.length);
  if (!lines.some(part => part.length)) throw new Error('No audio was generated.');
  const pauses = lines.map((_, index) => {
    const ms = Math.max(0, Math.min(5000, Number(segments[index] && segments[index].pause) || 0));
    return Math.round(sampleRate * ms / 1000);
  });
  const cues = [];
  let cursor = 0;
  lines.forEach((part, index) => {
    const start = cursor / sampleRate;
    cursor += part.length;
    cues.push({ start, end: cursor / sampleRate });
    if (index !== lines.length - 1) cursor += pauses[index];
  });
  return { blob: encodeWav(joinLines(lines, pauses), sampleRate), cues };
}
