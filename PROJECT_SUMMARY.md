# Translation App — Project Summary

> Real-time conference translation: Pi mic → STT → Translate → TTS → Web browser

---

## System Overview

```
Microphone (Pi) → VAD → WebSocket frames → FastAPI Backend → Google Cloud APIs → WebSocket → Browser
```

Three target languages processed **in parallel**: Swedish (`sv-SE`), Norwegian (`nb-NO`), German (`de-DE`). Source language: English (`en-US`).

---

## Components

| Component | Location | Runtime |
|-----------|----------|---------|
| Backend | `backend/main.py` | Mac Mini, port 8000 |
| Pi Client | `pi-client/main.py` | Raspberry Pi |
| Frontend | `frontend/index.html` | Any browser |

---

## Backend (`backend/main.py`)

FastAPI server. Key routes:

| Route | Method | Purpose |
|-------|--------|---------|
| `/ws/audio/{device_id}` | WebSocket | Receive PCM frames from Pi, run streaming STT |
| `/ws/{language_code}` | WebSocket | Web client subscribes to language channel |
| `/audio/chunk` | POST | HTTP fallback — receive WAV, run full STT+TTS pipeline |
| `/audio/{session_id}/{filename}` | GET | Serve MP3 files |
| `/app` | GET | Serve frontend HTML |

### Streaming STT Pipeline (`/ws/audio/{device_id}`)
1. Receive raw 16-bit PCM frames via WebSocket
2. Open Google STT streaming session on first speech
3. Broadcast **interim** results (text only, no TTS) to web clients
4. On **final** result: translate × 3 + TTS × 3 in parallel via `asyncio.gather()`
5. Broadcast full `{type: "translation", translation, audio_url}` message per language
6. Provisional chunking: if no final result for 4 seconds, emit provisional translation
7. Close STT stream on 5 seconds of silence

### Session Storage
```
backend/data/sessions/{YYYYMMDD_HHMMSS}/
├── config.json          # Session metadata
├── manifest.json        # Index of all processed chunks
└── chunks/
    ├── {id}_original.wav
    ├── {id}_transcript.txt
    └── {id}_{lang}.mp3
```

### WebSocket Message Format (to browser)
```json
{
  "type": "translation | interim | connected",
  "chunk_id": "001",
  "timestamp": "ISO8601",
  "original": "English text",
  "translation": "Translated text",
  "audio_url": "/audio/{session_id}/{chunk_id}_{lang}.mp3",
  "language": "sv-SE"
}
```

### Google Cloud Services
- `speech_v1.SpeechClient` — STT (Linear16, 16kHz mono)
- `translate_v2.Client` — Translation
- `texttospeech.TextToSpeechClient` — TTS (MP3 output)
- Credentials: `backend/config/service-account-credentials.json` (gitignored, set via `GOOGLE_APPLICATION_CREDENTIALS`)

---

## Pi Client (`pi-client/main.py`)

Captures audio, runs VAD, streams to backend.

### Classes
- **`AudioCapture`** — Opens mic, resamples to 16kHz if needed (numpy interpolation)
- **`VADProcessor`** (in `vad_processor.py`) — Silero VAD; `SimpleVAD` energy-based fallback
- **`StreamingTranslationClient`** — WebSocket client to `/ws/audio/{device_id}`, binary PCM frames, auto-reconnect (5s backoff)
- **`TranslationClient`** — Orchestrates: streams while speech detected, pauses after 5s silence, falls back to HTTP POST

### Audio Requirements
- 16kHz, 16-bit, mono PCM
- 32ms chunks = 512 samples (Silero VAD minimum)

### Key Config
```json
// pi-client/config.json
{
  "backend_url": "https://sw-translation.ngrok.app",
  "device_id": "pc_test_001",
  "device_index": null
}
```

### VAD Settings (`pi-client/config/settings.py`)
| Setting | Default | Notes |
|---------|---------|-------|
| `SILENCE_THRESHOLD` | 180ms | Silence before ending segment |
| `MAX_CHUNK_DURATION_MS` | 10000ms | Force-split long utterances |
| `CHUNK_DURATION_MS` | 32ms | Audio read size |
| `SAMPLE_RATE` | 16000 | Google STT requirement |

---

## Frontend (`frontend/index.html`)

Single HTML file. No build step.

### Key Behaviour
- Login screen: select language → connect to `/ws/{lang_code}`
- Typewriter effect synced to TTS audio duration
- Dynamic playback rate based on queue depth (1.0x → 1.75x)
- Auto-reconnect with exponential backoff
- Interim results shown live; replaced by final translation + audio

### Hardcoded URL
```javascript
const BACKEND_URL = 'wss://sw-translation.ngrok.app';  // change for local dev
```

### State Constants
```javascript
const MAX_QUEUE_DEPTH = 4;   // Flush stale items beyond this
const WORD_LIMIT = 300;      // Max visible words on screen
```

---

## Running the System

### Backend (Mac Mini)
```bash
cd backend
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS="./config/service-account-credentials.json"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Pi Client
```bash
cd pi-client
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Frontend
Open `http://MAC_IP:8000/app` in browser (or direct `frontend/index.html`).

---

## Dependencies

**Backend:**
```
fastapi==0.109.0, uvicorn[standard]==0.27.0, python-multipart==0.0.6,
websockets==12.0, google-cloud-speech==2.21.0, google-cloud-translate==3.12.1,
google-cloud-texttospeech==2.14.2, python-dotenv==1.0.0
```

**Pi Client:**
```
pyaudio==0.2.14, torch>=2.2.0, torchaudio>=2.2.0, numpy,
requests==2.31.0, websocket-client==1.7.0
```

---

## Architecture Decisions (Why)

| Decision | Reason |
|----------|--------|
| Streaming STT via WebSocket | Lower latency than batch HTTP |
| Provisional chunking (4s timeout) | Handles slow speakers / long pauses |
| Parallel translate+TTS via `asyncio.gather` | All 3 languages in ~same time as 1 |
| Dynamic audio playback rate | Prevents queue buildup lag |
| Single HTML frontend | No build step, easy to share |
| File-based session storage | MVP simplicity, no database needed |
| Silero VAD + SimpleVAD fallback | Silero is more accurate; fallback for Pi without torch |

---

## Current Git State (as of 2026-05-30)

- **Active branch:** `feature/streaming-stt`
- **Main branch:** `master`
- **Recent commits:**
  - `4a6b744` split at comma
  - `cae2385` Add streaming STT pipeline via WebSocket (WIP)
  - `6384c2f` Process translate+TTS in parallel, drain stale queue
  - `92c9d3a` serve FE from static
  - `dbfb342` initial commit

---

## Known Issues / WIP

- Streaming STT branch (`feature/streaming-stt`) marked as WIP — needs further debugging
- `backend/config/config.json` and `config.local.json` exist but are not yet used by `main.py`
- No authentication on any endpoint (MVP mode, CORS allow-all)
