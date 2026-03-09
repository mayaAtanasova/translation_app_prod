"""
Translation System Backend
FastAPI server for real-time conference translation
Handles audio chunks from Raspberry Pi devices and serves translations to web clients

v2.0 Changes:
- Chunk buffering: accumulates N transcripts before translating (better sentence context)
- Timeout flush: buffer flushes after X seconds even if not full (handles slow speakers)
- Context injection: previous buffer text prepended to translation call (cross-chunk coherence)
- Gemini display smoother: async worker writes smoothed display.txt per language every N seconds
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import json
import os
from datetime import datetime
from pathlib import Path
import asyncio
from typing import Dict, Set, List, Optional
import logging
import time

# Google Cloud imports
from google.cloud import speech_v1
from google.cloud import translate_v2 as translate
from google.cloud import texttospeech
from google import genai
gemini_client = genai.Client()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loader — merges config.json (defaults) with config.local.json (secrets)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path("./config/config.json")
    local_path = Path("./config/config.local.json")

    config = {}

    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        logger.info("Loaded config.json")
    else:
        logger.warning("config.json not found — using defaults")

    if local_path.exists():
        with open(local_path) as f:
            local = json.load(f)
        config.update(local)
        logger.info("Loaded config.local.json (secrets merged)")
    else:
        logger.warning("config.local.json not found — no secrets loaded")

    return config

CONFIG = load_config()

# ---------------------------------------------------------------------------
# App settings (from config with sensible fallbacks)
# ---------------------------------------------------------------------------

BUFFER_SIZE             = CONFIG.get("buffer_size", 3)
BUFFER_TIMEOUT_SECONDS  = CONFIG.get("buffer_timeout_seconds", 8)
DISPLAY_REFRESH_SECONDS = CONFIG.get("display_refresh_seconds", 15)
GEMINI_MODEL            = CONFIG.get("gemini_model", "gemini-1.5-flash")
GOOGLE_PROJECT_ID       = CONFIG.get("google_project_id", "")

# ---------------------------------------------------------------------------
# Gemini setup
# ---------------------------------------------------------------------------

# Gemini uses Application Default Credentials (same service account as other Google APIs)
# No separate API key needed — it authenticates via GOOGLE_APPLICATION_CREDENTIALS
genai.configure()  # picks up ADC automatically
gemini_model = genai.GenerativeModel(GEMINI_MODEL)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Translation System Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

DATA_DIR     = Path("./data")
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Language config
# ---------------------------------------------------------------------------

TARGET_LANGUAGES = {
    'sv-SE': {'name': 'Swedish',   'translate_code': 'sv', 'tts_code': 'sv-SE'},
    'nb-NO': {'name': 'Norwegian', 'translate_code': 'no', 'tts_code': 'nb-NO'},
    'de-DE': {'name': 'German',    'translate_code': 'de', 'tts_code': 'de-DE'},
}

# ---------------------------------------------------------------------------
# WebSocket connections
# ---------------------------------------------------------------------------

active_connections: Dict[str, Set[WebSocket]] = {
    lang: set() for lang in TARGET_LANGUAGES
}

# ---------------------------------------------------------------------------
# Google Cloud clients
# ---------------------------------------------------------------------------

stt_client       = speech_v1.SpeechClient()
translate_client = translate.Client()
tts_client       = texttospeech.TextToSpeechClient()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

current_session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# TranscriptBuffer
# Holds incoming transcripts until buffer is full OR timeout fires.
# Thread-safe via asyncio.Lock.
# ---------------------------------------------------------------------------

class TranscriptBuffer:
    """
    Accumulates transcripts for a session and flushes when:
      - buffer reaches BUFFER_SIZE chunks, OR
      - BUFFER_TIMEOUT_SECONDS have elapsed since the first chunk was added

    Also maintains a rolling context string (last flushed text) for
    context injection into the next translation call.
    """

    def __init__(self):
        self.chunks: List[dict] = []          # [{chunk_id, transcript, timestamp}, ...]
        self.first_chunk_time: Optional[float] = None
        self.lock = asyncio.Lock()

        # Context memory: last translated English text per language
        # Used to give the translation API cross-buffer coherence
        self.last_context: Dict[str, str] = {lang: "" for lang in TARGET_LANGUAGES}

        # Rolling translated text per language (for display smoother)
        # Stores list of translated strings in order
        self.translation_history: Dict[str, List[str]] = {lang: [] for lang in TARGET_LANGUAGES}

    async def add(self, chunk_id: str, transcript: str) -> Optional[List[dict]]:
        """
        Add a transcript to the buffer.
        Returns the flushed chunk list if buffer is ready, else None.
        """
        async with self.lock:
            self.chunks.append({
                "chunk_id": chunk_id,
                "transcript": transcript,
                "timestamp": datetime.now().isoformat()
            })

            if self.first_chunk_time is None:
                self.first_chunk_time = time.monotonic()

            if len(self.chunks) >= BUFFER_SIZE:
                logger.info(f"Buffer full ({BUFFER_SIZE} chunks) — flushing")
                return self._flush()

        return None  # not ready yet

    async def flush_if_timeout(self) -> Optional[List[dict]]:
        """
        Called by the timeout watcher. Flushes if buffer has content
        and has been waiting longer than BUFFER_TIMEOUT_SECONDS.
        """
        async with self.lock:
            if not self.chunks:
                return None

            elapsed = time.monotonic() - self.first_chunk_time
            if elapsed >= BUFFER_TIMEOUT_SECONDS:
                logger.info(f"Buffer timeout ({elapsed:.1f}s) — flushing {len(self.chunks)} chunk(s)")
                return self._flush()

        return None

    def _flush(self) -> List[dict]:
        """Internal flush — must be called with lock held."""
        flushed = self.chunks.copy()
        self.chunks = []
        self.first_chunk_time = None
        return flushed

    def update_context(self, lang_code: str, translated_text: str):
        """Store last translated text as context for next buffer."""
        self.last_context[lang_code] = translated_text

    def get_context(self, lang_code: str) -> str:
        """Get context string for a language."""
        return self.last_context.get(lang_code, "")

    def add_to_history(self, lang_code: str, translated_text: str):
        """Append translated text to rolling history for display smoother."""
        self.translation_history[lang_code].append(translated_text)

    def get_recent_history(self, lang_code: str, word_limit: int = 400) -> str:
        """
        Return the most recent translations up to ~word_limit words.
        Works backwards through history so we always have the latest.
        """
        history = self.translation_history[lang_code]
        if not history:
            return ""

        collected = []
        word_count = 0

        for text in reversed(history):
            words = text.split()
            if word_count + len(words) > word_limit:
                break
            collected.insert(0, text)
            word_count += len(words)

        return " ".join(collected)


# One buffer per session (reset on new session)
transcript_buffer = TranscriptBuffer()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_session():
    global current_session_id, transcript_buffer

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_session_id = timestamp

    session_dir = SESSIONS_DIR / current_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "chunks").mkdir(exist_ok=True)

    # Create language display dirs
    for lang_code in TARGET_LANGUAGES:
        (session_dir / "display" / lang_code).mkdir(parents=True, exist_ok=True)

    config = {
        "session_id": current_session_id,
        "start_time": datetime.now().isoformat(),
        "device_id": "pi_device_001",
        "source_language": "en-US",
        "target_languages": list(TARGET_LANGUAGES.keys()),
        "buffer_size": BUFFER_SIZE,
        "buffer_timeout_seconds": BUFFER_TIMEOUT_SECONDS,
    }
    with open(session_dir / "config.json", 'w') as f:
        json.dump(config, f, indent=2)

    manifest = {"session_id": current_session_id, "chunks": [], "buffer_groups": []}
    with open(session_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f, indent=2)

    # Fresh buffer for new session
    transcript_buffer = TranscriptBuffer()

    logger.info(f"Created session: {current_session_id}")
    return current_session_id


def get_current_session():
    global current_session_id
    if not current_session_id:
        return create_session()
    return current_session_id


def update_manifest(chunk_data: dict):
    session_id = get_current_session()
    manifest_path = SESSIONS_DIR / session_id / "manifest.json"
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    manifest['chunks'].append(chunk_data)
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)


def update_manifest_buffer_group(group_data: dict):
    """Record each buffer flush as a group in the manifest."""
    session_id = get_current_session()
    manifest_path = SESSIONS_DIR / session_id / "manifest.json"
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    manifest['buffer_groups'].append(group_data)
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------

async def broadcast_translation(language_code: str, data: dict):
    if language_code not in active_connections:
        return
    dead = set()
    for ws in active_connections[language_code]:
        try:
            await ws.send_json(data)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            dead.add(ws)
    active_connections[language_code] -= dead


# ---------------------------------------------------------------------------
# Core translation pipeline
# Called when buffer flushes (either trigger)
# ---------------------------------------------------------------------------

async def process_buffer_group(chunks: List[dict]):
    """
    Translate a group of buffered transcripts together.
    - Joins transcripts into one string for better sentence context
    - Injects previous buffer text as context
    - Runs TTS on the combined translation
    - Broadcasts to clients
    - Updates translation history for display smoother
    """
    session_id = get_current_session()
    chunks_dir = SESSIONS_DIR / session_id / "chunks"

    # Combine transcripts into one body of text
    combined_transcript = " ".join(c["transcript"] for c in chunks)
    group_id = chunks[0]["chunk_id"]  # use first chunk ID as group label

    logger.info(f"Processing buffer group {group_id}: \"{combined_transcript}\"")

    group_data = {
        "group_id": group_id,
        "timestamp": datetime.now().isoformat(),
        "chunk_ids": [c["chunk_id"] for c in chunks],
        "combined_transcript": combined_transcript,
        "translations": {}
    }

    for lang_code, lang_info in TARGET_LANGUAGES.items():
        try:
            # --- Context injection ---
            context = transcript_buffer.get_context(lang_code)
            if context:
                # Prepend context so the translation engine understands what came before
                text_to_translate = f"{context} {combined_transcript}"
                logger.info(f"[{lang_code}] Translating with context ({len(context.split())} context words)")
            else:
                text_to_translate = combined_transcript

            # --- Translation ---
            translation_result = translate_client.translate(
                text_to_translate,
                target_language=lang_info['translate_code'],
                source_language='en'
            )
            full_translated = translation_result['translatedText']

            # Strip the context portion from the output
            # We only want the translation of the NEW text, not the context
            if context:
                # Translate context alone to find the boundary
                context_translation = translate_client.translate(
                    context,
                    target_language=lang_info['translate_code'],
                    source_language='en'
                )['translatedText']

                # Remove context translation prefix if present
                if full_translated.startswith(context_translation):
                    translated_text = full_translated[len(context_translation):].strip()
                else:
                    # Fallback: translate without context (context still helped the model)
                    translated_text = translate_client.translate(
                        combined_transcript,
                        target_language=lang_info['translate_code'],
                        source_language='en'
                    )['translatedText']
            else:
                translated_text = full_translated

            logger.info(f"[{lang_code}] {translated_text}")

            # Update context for next buffer
            transcript_buffer.update_context(lang_code, combined_transcript)

            # Add to rolling history for display smoother
            transcript_buffer.add_to_history(lang_code, translated_text)

            # --- TTS ---
            synthesis_input = texttospeech.SynthesisInput(text=translated_text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang_info['tts_code'],
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
            )
            audio_config_tts = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )
            tts_response = tts_client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config_tts
            )

            # Save audio — named by group_id and lang
            audio_filename = f"{group_id}_{lang_code}.mp3"
            audio_path = chunks_dir / audio_filename
            with open(audio_path, 'wb') as f:
                f.write(tts_response.audio_content)

            group_data['translations'][lang_code] = {
                "text": translated_text,
                "audio_url": f"/audio/{session_id}/{audio_filename}"
            }

            # Broadcast to live clients
            await broadcast_translation(lang_code, {
                "type": "translation",
                "group_id": group_id,
                "chunk_ids": [c["chunk_id"] for c in chunks],
                "timestamp": group_data['timestamp'],
                "original": combined_transcript,
                "translation": translated_text,
                "audio_url": group_data['translations'][lang_code]['audio_url'],
                "language": lang_code
            })

        except Exception as e:
            logger.error(f"Error processing language {lang_code}: {e}")

    update_manifest_buffer_group(group_data)
    logger.info(f"Buffer group {group_id} processed successfully")


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def buffer_timeout_watcher():
    """
    Runs continuously. Checks every second if any buffered chunks
    have been waiting longer than BUFFER_TIMEOUT_SECONDS.
    """
    logger.info("Buffer timeout watcher started")
    while True:
        await asyncio.sleep(1)
        try:
            flushed = await transcript_buffer.flush_if_timeout()
            if flushed:
                await process_buffer_group(flushed)
        except Exception as e:
            logger.error(f"Buffer timeout watcher error: {e}")


async def display_smoother():
    """
    Runs every DISPLAY_REFRESH_SECONDS.
    Takes rolling translation history per language, sends to Gemini
    to produce a clean, coherent prose version, writes to display.txt.
    FE polls this file for the smoothed transcript panel.
    """
    logger.info("Display smoother started")
    await asyncio.sleep(DISPLAY_REFRESH_SECONDS)  # initial delay

    while True:
        try:
            session_id = get_current_session()

            for lang_code, lang_info in TARGET_LANGUAGES.items():
                raw_text = transcript_buffer.get_recent_history(lang_code, word_limit=400)

                if not raw_text.strip():
                    continue

                prompt = (
                    f"You are a professional transcript editor. "
                    f"The following text is a live translation into {lang_info['name']} "
                    f"from a conference speech. It was produced in small chunks and may contain "
                    f"broken sentences, repetitions, or awkward phrasing. "
                    f"Please rewrite it as clean, coherent prose. "
                    f"Do not add any commentary, introduction, or explanation — "
                    f"return only the cleaned text.\n\n"
                    f"{raw_text}"
                )

                try:
                    response = gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=prompt
                    )
                    smoothed_text = response.text.strip()

                    # Write to display.txt for this language
                    display_path = (
                        SESSIONS_DIR / session_id / "display" / lang_code / "display.txt"
                    )
                    with open(display_path, 'w', encoding='utf-8') as f:
                        f.write(smoothed_text)

                    logger.info(f"[{lang_code}] display.txt updated ({len(smoothed_text.split())} words)")

                except Exception as e:
                    logger.error(f"Gemini error for {lang_code}: {e}")

        except Exception as e:
            logger.error(f"Display smoother error: {e}")

        await asyncio.sleep(DISPLAY_REFRESH_SECONDS)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    logger.info("Backend v2.0 starting up...")
    logger.info(f"Buffer size: {BUFFER_SIZE} chunks, timeout: {BUFFER_TIMEOUT_SECONDS}s")
    logger.info(f"Display refresh: {DISPLAY_REFRESH_SECONDS}s, Gemini model: {GEMINI_MODEL}")

    create_session()

    # Start background tasks
    asyncio.create_task(buffer_timeout_watcher())
    asyncio.create_task(display_smoother())

    logger.info(f"Session: {current_session_id}")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "status": "running",
        "version": "2.0.0",
        "session": current_session_id,
        "languages": list(TARGET_LANGUAGES.keys()),
        "buffer_size": BUFFER_SIZE,
        "buffer_timeout_seconds": BUFFER_TIMEOUT_SECONDS,
    }


@app.post("/audio/chunk")
async def receive_audio_chunk(
    audio: UploadFile = File(...),
    chunk_id: str = None
):
    """
    Receive audio chunk from Raspberry Pi.
    Step 1: STT (immediate, per chunk)
    Step 2: Add transcript to buffer
    Step 3: If buffer flushes — translate + TTS + broadcast
    """
    try:
        session_id = get_current_session()

        if not chunk_id:
            chunk_id = f"{len(os.listdir(SESSIONS_DIR / session_id / 'chunks')) + 1:03d}"

        logger.info(f"Receiving chunk {chunk_id}")

        # Save original audio
        chunks_dir = SESSIONS_DIR / session_id / "chunks"
        original_path = chunks_dir / f"{chunk_id}_original.wav"

        audio_data = await audio.read()
        with open(original_path, 'wb') as f:
            f.write(audio_data)

        # STT — still per-chunk, immediate
        audio_content = speech_v1.RecognitionAudio(content=audio_data)
        stt_config = speech_v1.RecognitionConfig(
            encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,  # important for translation quality
        )

        response = stt_client.recognize(config=stt_config, audio=audio_content)

        if not response.results:
            logger.warning(f"No speech in chunk {chunk_id}")
            return {"status": "no_speech", "chunk_id": chunk_id}

        transcript = response.results[0].alternatives[0].transcript
        confidence = response.results[0].alternatives[0].confidence

        logger.info(f"Chunk {chunk_id} transcript: \"{transcript}\" ({confidence:.0%})")

        # Save transcript file
        with open(chunks_dir / f"{chunk_id}_transcript.txt", 'w', encoding='utf-8') as f:
            f.write(f"Confidence: {confidence:.2%}\n")
            f.write(f"Transcript: {transcript}\n")

        # Record in manifest (individual chunk level)
        update_manifest({
            "chunk_id": chunk_id,
            "timestamp": datetime.now().isoformat(),
            "transcript": transcript,
            "confidence": confidence,
        })

        # Add to buffer — may trigger a flush
        flushed = await transcript_buffer.add(chunk_id, transcript)

        if flushed:
            # Process in background so we return quickly to the Pi
            asyncio.create_task(process_buffer_group(flushed))
            return {
                "status": "buffered_and_flushed",
                "chunk_id": chunk_id,
                "transcript": transcript,
                "buffer_group_size": len(flushed),
            }
        else:
            buffer_count = len(transcript_buffer.chunks)
            logger.info(f"Chunk {chunk_id} buffered ({buffer_count}/{BUFFER_SIZE})")
            return {
                "status": "buffered",
                "chunk_id": chunk_id,
                "transcript": transcript,
                "buffer_position": buffer_count,
                "buffer_size": BUFFER_SIZE,
            }

    except Exception as e:
        logger.error(f"Error processing chunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/{language_code}")
async def websocket_endpoint(websocket: WebSocket, language_code: str):
    if language_code not in TARGET_LANGUAGES:
        await websocket.close(code=1003)
        return

    await websocket.accept()
    active_connections[language_code].add(websocket)
    logger.info(f"Client connected [{language_code}]. Total: {len(active_connections[language_code])}")

    await websocket.send_json({
        "type": "connected",
        "language": language_code,
        "session": current_session_id
    })

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        active_connections[language_code].remove(websocket)
        logger.info(f"Client disconnected [{language_code}]. Remaining: {len(active_connections[language_code])}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        active_connections[language_code].discard(websocket)


@app.get("/audio/{session_id}/{filename}")
async def serve_audio(session_id: str, filename: str):
    audio_path = SESSIONS_DIR / session_id / "chunks" / filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(audio_path)


@app.get("/sessions/{session_id}/manifest")
async def get_session_manifest(session_id: str):
    manifest_path = SESSIONS_DIR / session_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    with open(manifest_path, 'r') as f:
        return json.load(f)


@app.get("/sessions/{session_id}/display/{language_code}")
async def get_display_text(session_id: str, language_code: str):
    """
    Serve the smoothed display.txt for a language.
    FE polls this endpoint for the transcript panel.
    """
    if language_code not in TARGET_LANGUAGES:
        raise HTTPException(status_code=400, detail="Unknown language code")

    display_path = SESSIONS_DIR / session_id / "display" / language_code / "display.txt"

    if not display_path.exists():
        return {"status": "no_content", "text": ""}

    with open(display_path, 'r', encoding='utf-8') as f:
        text = f.read()

    return {"status": "ok", "language": language_code, "text": text}


@app.post("/session/new")
async def create_new_session():
    session_id = create_session()
    return {"session_id": session_id}


@app.get("/session/status")
async def session_status():
    """Debug endpoint — shows current buffer state."""
    return {
        "session_id": current_session_id,
        "buffer_chunks_waiting": len(transcript_buffer.chunks),
        "buffer_size": BUFFER_SIZE,
        "buffer_timeout_seconds": BUFFER_TIMEOUT_SECONDS,
        "context_lengths": {
            lang: len(transcript_buffer.get_context(lang).split())
            for lang in TARGET_LANGUAGES
        },
        "history_lengths": {
            lang: len(transcript_buffer.translation_history[lang])
            for lang in TARGET_LANGUAGES
        }
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)