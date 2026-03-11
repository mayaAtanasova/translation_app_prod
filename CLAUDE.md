# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the System

### Backend (Mac Mini)
```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export GOOGLE_APPLICATION_CREDENTIALS="./config/service-account-credentials.json"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Pi Client (Raspberry Pi)
```bash
cd pi-client
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Frontend
The frontend (`frontend/index.html`) is a single HTML file served by the backend at `/app` (via `static/index.html`). Open it directly in a browser or access via `http://MAC_IP:8000/app`.

## Architecture

**Data flow:** Pi mic → VAD → HTTP POST WAV → Backend STT → Translate (3x) → TTS (3x) → WebSocket broadcast → Frontend

**Backend (`backend/main.py`):** Single FastAPI server. On receiving a WAV chunk at `POST /audio/chunk`:
1. Saves the original WAV to `data/sessions/{session_id}/chunks/`
2. Calls Google Cloud STT (synchronous, run in thread via `asyncio.to_thread`)
3. In parallel: translates + synthesizes TTS for all 3 languages using `asyncio.gather`
4. Saves MP3s and broadcasts to language-specific WebSocket channels (`/ws/{language_code}`)
5. Audio files served at `GET /audio/{session_id}/{filename}`

Sessions auto-create on startup and are tracked via global `current_session_id`. Each session has a `manifest.json` listing all processed chunks.

**Frontend (`frontend/index.html`):** Single HTML file. Login screen selects language, then connects to the appropriate `wss://` WebSocket channel. Displays translations with typewriter effect synced to TTS audio playback.

**Pi client (`pi-client/main.py`):** Uses `VADProcessor` (Silero VAD via torch.hub) to detect speech segments. Sends completed segments as WAV via HTTP POST in background threads to avoid blocking audio capture. Falls back to `SimpleVAD` (energy-based) if Silero fails.

## Configuration

**Backend languages** are hardcoded in `backend/main.py` `TARGET_LANGUAGES` dict:
- `sv-SE` (Swedish), `nb-NO` (Norwegian), `de-DE` (German)

**Pi client backend URL** — set in `pi-client/config.json`:
```json
{ "backend_url": "http://192.168.1.100:8000", "device_id": "pi_device_001", "device_index": null }
```

**Frontend WebSocket URL** — hardcoded in `frontend/index.html` as `const BACKEND_URL` (uses `wss://sw-translation.ngrok.app` for production ngrok tunnel).

**VAD tuning** — `pi-client/config/settings.py`:
- `SILENCE_THRESHOLD` (ms): silence before ending a speech segment (default 180ms)
- `MAX_CHUNK_DURATION_MS`: force-split long utterances (default 10000ms)
- `CHUNK_DURATION_MS`: audio read size (default 32ms, must yield ≥512 samples for Silero)

**Google credentials** live at `backend/config/service-account-credentials.json` (gitignored).

## Key Constraints

- Google Cloud STT requires 16kHz mono LINEAR16 WAV. The Pi captures at 16kHz, 16-bit, mono.
- Silero VAD requires minimum 512-sample chunks (1024 bytes at 16-bit). The 32ms chunk size at 16kHz yields exactly 512 samples.
- The backend's `static/` directory must exist for the frontend mount to work (`app.mount("/static", StaticFiles(directory="static"), name="static")`).
- WebSocket clients must send `"ping"` to receive `"pong"` keepalives; reconnect logic is in the frontend.
