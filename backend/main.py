"""
Translation System Backend
FastAPI server for real-time conference translation
Handles audio chunks from Raspberry Pi devices and serves translations to web clients
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
import uvicorn
import json
import os
from datetime import datetime
from pathlib import Path
import asyncio
import queue as thread_queue
from typing import Dict, Set
import logging
from dotenv import load_dotenv
load_dotenv()

# Google Cloud imports
from google.cloud import speech_v1
from google.cloud import translate_v2 as translate
from google.cloud import texttospeech

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Translation System Backend", version="1.0.0")

# CORS middleware for web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = os.getenv("STATIC_DIR", "static")

# Target languages for MVP
TARGET_LANGUAGES = {
    'sv-SE': {'name': 'Swedish', 'translate_code': 'sv', 'tts_code': 'sv-SE'},
    'nb-NO': {'name': 'Norwegian', 'translate_code': 'no', 'tts_code': 'nb-NO'},
    'de-DE': {'name': 'German', 'translate_code': 'de', 'tts_code': 'de-DE'},
}

# Active WebSocket connections (language -> set of websockets)
active_connections: Dict[str, Set[WebSocket]] = {
    'sv-SE': set(),
    'nb-NO': set(),
    'de-DE': set(),
}

# Google Cloud clients
stt_client = speech_v1.SpeechClient()
translate_client = translate.Client()
tts_client = texttospeech.TextToSpeechClient()

# Current session (hardcoded for MVP)
current_session_id = None


def create_session():
    """Create a new translation session"""
    global current_session_id
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_session_id = timestamp
    
    session_dir = SESSIONS_DIR / current_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    
    # Create chunks subdirectory
    chunks_dir = session_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    
    # Create config file
    config = {
        "session_id": current_session_id,
        "start_time": datetime.now().isoformat(),
        "device_id": "pi_device_001",  # Hardcoded for MVP
        "source_language": "en-US",
        "target_languages": list(TARGET_LANGUAGES.keys()),
    }
    
    with open(session_dir / "config.json", 'w') as f:
        json.dump(config, f, indent=2)
    
    # Create manifest
    manifest = {
        "session_id": current_session_id,
        "chunks": []
    }
    
    with open(session_dir / "manifest.json", 'w') as f:
        json.dump(manifest, f, indent=2)
    
    logger.info(f"Created session: {current_session_id}")
    return current_session_id


def get_current_session():
    """Get or create current session"""
    global current_session_id
    
    if not current_session_id:
        return create_session()
    
    return current_session_id


def update_manifest(chunk_data):
    """Add chunk to session manifest"""
    session_id = get_current_session()
    manifest_path = SESSIONS_DIR / session_id / "manifest.json"
    
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    
    manifest['chunks'].append(chunk_data)
    
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)


async def broadcast_translation(language_code: str, data: dict):
    """Send translation to all connected clients for a language"""
    if language_code not in active_connections:
        return
    
    dead_connections = set()
    
    for websocket in active_connections[language_code]:
        try:
            await websocket.send_json(data)
        except Exception as e:
            logger.error(f"Error sending to websocket: {e}")
            dead_connections.add(websocket)
    
    # Remove dead connections
    active_connections[language_code] -= dead_connections


@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("Backend starting up...")
    
    # Create initial session
    create_session()
    
    logger.info(f"Current session: {current_session_id}")


@app.get("/")
async def root():
    return RedirectResponse(url="/app")

@app.get("/health")
async def health():
    return {
        "status": "running",
        "session": current_session_id,
        "languages": list(TARGET_LANGUAGES.keys())
    }


@app.post("/audio/chunk")
async def receive_audio_chunk(
    audio: UploadFile = File(...),
    chunk_id: str = None
):
    """
    Receive audio chunk from Raspberry Pi
    Process: STT -> Translation -> TTS -> Broadcast
    """
    try:
        session_id = get_current_session()
        
        # Generate chunk ID if not provided
        if not chunk_id:
            chunk_id = f"{len(os.listdir(SESSIONS_DIR / session_id / 'chunks')) + 1:03d}"
        
        logger.info(f"Receiving chunk {chunk_id}")
        
        # Save original audio
        chunks_dir = SESSIONS_DIR / session_id / "chunks"
        original_path = chunks_dir / f"{chunk_id}_original.wav"
        
        audio_data = await audio.read()
        with open(original_path, 'wb') as f:
            f.write(audio_data)
        
        # Step 1: Speech-to-Text
        logger.info(f"Transcribing chunk {chunk_id}...")
        
        audio_content = speech_v1.RecognitionAudio(content=audio_data)
        config = speech_v1.RecognitionConfig(
            encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
        )
        
        response = stt_client.recognize(config=config, audio=audio_content)
        
        if not response.results:
            logger.warning(f"No speech detected in chunk {chunk_id}")
            return {"status": "no_speech", "chunk_id": chunk_id}
        
        transcript = response.results[0].alternatives[0].transcript
        confidence = response.results[0].alternatives[0].confidence
        
        logger.info(f"Transcript: {transcript}")
        
        # Save transcript
        transcript_path = chunks_dir / f"{chunk_id}_transcript.txt"
        with open(transcript_path, 'w', encoding='utf-8') as f:
            f.write(f"Confidence: {confidence:.2%}\n")
            f.write(f"Transcript: {transcript}\n")
        
        # Step 2 & 3: Translate and synthesize for all languages in parallel
        chunk_data = {
            "chunk_id": chunk_id,
            "timestamp": datetime.now().isoformat(),
            "transcript": transcript,
            "confidence": confidence,
            "translations": {}
        }

        async def process_language(lang_code, lang_info):
            """Translate + TTS + save + broadcast for one language."""
            logger.info(f"Processing {lang_info['name']}...")

            translation_result = await asyncio.to_thread(
                translate_client.translate,
                transcript,
                target_language=lang_info['translate_code'],
                source_language='en'
            )
            translated_text = translation_result['translatedText']
            logger.info(f"{lang_info['name']}: {translated_text}")

            synthesis_input = texttospeech.SynthesisInput(text=translated_text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang_info['tts_code'],
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )
            tts_response = await asyncio.to_thread(
                tts_client.synthesize_speech,
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )

            audio_path = chunks_dir / f"{chunk_id}_{lang_code}.mp3"
            with open(audio_path, 'wb') as f:
                f.write(tts_response.audio_content)

            audio_url = f"/audio/{session_id}/{chunk_id}_{lang_code}.mp3"

            await broadcast_translation(lang_code, {
                "type": "translation",
                "chunk_id": chunk_id,
                "timestamp": chunk_data['timestamp'],
                "original": transcript,
                "translation": translated_text,
                "audio_url": audio_url,
                "language": lang_code
            })

            return lang_code, translated_text, audio_url

        results = await asyncio.gather(*[
            process_language(lang_code, lang_info)
            for lang_code, lang_info in TARGET_LANGUAGES.items()
        ])

        for lang_code, translated_text, audio_url in results:
            chunk_data['translations'][lang_code] = {
                "text": translated_text,
                "audio_url": audio_url
            }
        
        # Update manifest
        update_manifest(chunk_data)
        
        logger.info(f"Chunk {chunk_id} processed successfully")
        
        return {
            "status": "success",
            "chunk_id": chunk_id,
            "transcript": transcript,
            "translations": chunk_data['translations']
        }
        
    except Exception as e:
        logger.error(f"Error processing chunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _split_at_comma(text: str) -> str:
    """
    Return text up to the last comma, so provisional TTS ends at a natural
    clause boundary. Falls back to the full text if no suitable comma exists.
    """
    last_comma = text.rfind(',')
    if last_comma > 15:      # ignore commas too close to the start
        return text[:last_comma].strip()
    return text.strip()


async def broadcast_interim_translation(lang_code: str, lang_info: dict, transcript: str):
    """Translate and broadcast an interim result — no TTS, no file I/O."""
    try:
        result = await asyncio.to_thread(
            translate_client.translate,
            transcript,
            target_language=lang_info['translate_code'],
            source_language='en',
        )
        await broadcast_translation(lang_code, {
            "type": "interim",
            "translation": result['translatedText'],
            "original": transcript,
            "language": lang_code,
        })
    except Exception as e:
        logger.error(f"Interim translation error ({lang_code}): {e}")


async def process_language_streaming(
    lang_code: str, lang_info: dict, transcript: str, chunk_id: str, session_id: str
):
    """Full translate + TTS pipeline for a streaming final result."""
    try:
        translation_result = await asyncio.to_thread(
            translate_client.translate,
            transcript,
            target_language=lang_info['translate_code'],
            source_language='en',
        )
        translated_text = translation_result['translatedText']

        synthesis_input = texttospeech.SynthesisInput(text=translated_text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=lang_info['tts_code'],
            ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
        )
        audio_cfg = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3
        )
        tts_response = await asyncio.to_thread(
            tts_client.synthesize_speech,
            input=synthesis_input,
            voice=voice,
            audio_config=audio_cfg,
        )

        chunks_dir = SESSIONS_DIR / session_id / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        audio_path = chunks_dir / f"{chunk_id}_{lang_code}.mp3"
        with open(audio_path, 'wb') as f:
            f.write(tts_response.audio_content)

        audio_url = f"/audio/{session_id}/{chunk_id}_{lang_code}.mp3"
        await broadcast_translation(lang_code, {
            "type": "translation",
            "chunk_id": chunk_id,
            "timestamp": datetime.now().isoformat(),
            "original": transcript,
            "translation": translated_text,
            "audio_url": audio_url,
            "language": lang_code,
        })
    except Exception as e:
        logger.error(f"Streaming language processing error ({lang_code}): {e}")


@app.websocket("/ws/audio/{device_id}")
async def audio_stream_endpoint(websocket: WebSocket, device_id: str):
    """
    WebSocket endpoint for Raspberry Pi streaming audio.
    Pi sends raw 16kHz/16-bit/mono PCM frames while VAD is triggered.
    Backend pipes them to Google STT Streaming API for real-time results.
    """
    await websocket.accept()
    logger.info(f"Streaming audio connection from device: {device_id}")

    session_id = get_current_session()
    loop = asyncio.get_running_loop()

    # threading.Queue bridges the async WebSocket receiver and the sync STT thread
    audio_q: thread_queue.Queue = thread_queue.Queue()
    # asyncio.Queue carries STT results back to the async result processor
    result_q: asyncio.Queue = asyncio.Queue()

    streaming_config = speech_v1.StreamingRecognitionConfig(
        config=speech_v1.RecognitionConfig(
            encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
    )

    def stt_worker():
        """
        Runs in thread pool. Manages STT stream sessions across speech bursts.
        Blocks until audio arrives before opening a stream (avoids idle-timeout),
        then closes the stream after STREAM_SILENCE_TIMEOUT seconds of no audio
        and restarts automatically for the next burst.
        """
        STREAM_SILENCE_TIMEOUT = 5  # seconds of queue silence before closing stream

        while True:
            # Block until audio arrives — don't open a stream while the room is quiet
            first_chunk = audio_q.get()
            if first_chunk is None:
                break   # permanent stop from receive_audio()

            def request_gen():
                yield speech_v1.StreamingRecognizeRequest(audio_content=first_chunk)
                while True:
                    try:
                        chunk = audio_q.get(timeout=STREAM_SILENCE_TIMEOUT)
                    except thread_queue.Empty:
                        logger.debug("STT stream closing after silence, will restart on next speech")
                        return
                    if chunk is None:
                        audio_q.put(None)   # re-queue so outer loop sees the stop
                        return
                    yield speech_v1.StreamingRecognizeRequest(audio_content=chunk)

            try:
                for response in stt_client.streaming_recognize(streaming_config, request_gen()):
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        asyncio.run_coroutine_threadsafe(
                            result_q.put({
                                "transcript": result.alternatives[0].transcript,
                                "is_final": result.is_final,
                                "confidence": result.alternatives[0].confidence if result.is_final else None,
                            }),
                            loop,
                        )
            except Exception as e:
                logger.error(f"STT streaming error: {e}")
                # Loop back and wait for next audio regardless of error type

        asyncio.run_coroutine_threadsafe(result_q.put(None), loop)

    async def receive_audio():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"]:
                    audio_q.put(msg["bytes"])
                elif "text" in msg:
                    try:
                        data = json.loads(msg["text"])
                        if data.get("type") == "ping":
                            await websocket.send_text("pong")
                    except Exception:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"Audio receive error: {e}")
        finally:
            audio_q.put(None)   # signal STT thread to stop

    async def process_results():
        PROVISIONAL_TIMEOUT = 4   # seconds since last is_final before forcing interim

        chunk_counter = 0
        last_interim_words = 0
        last_interim_text = ""
        # Tracks chars of the current Google utterance already provisionally
        # processed, so the eventual is_final only TTSes the remainder.
        provisional_offset = 0
        # Wall-clock time of the last is_final (or startup) — provisional fires
        # when an interim arrives and this is more than PROVISIONAL_TIMEOUT ago.
        last_final_time = loop.time()

        while True:
            result = await result_q.get()
            if result is None:
                break

            transcript = result["transcript"].strip()
            if not transcript:
                continue

            if result["is_final"]:
                # Only TTS the part not already provisionally sent
                remaining = transcript[provisional_offset:].strip()
                chunk_counter += 1
                chunk_id = f"s{chunk_counter:03d}"
                last_interim_words = 0
                last_interim_text = ""
                provisional_offset = 0
                last_final_time = loop.time()
                logger.info(f"Final transcript [{device_id}]: {transcript}")

                if remaining:
                    await asyncio.gather(*[
                        process_language_streaming(lang_code, lang_info, remaining, chunk_id, session_id)
                        for lang_code, lang_info in TARGET_LANGUAGES.items()
                    ])
                else:
                    logger.info(f"Final transcript [{device_id}] fully covered by provisional chunks")
            else:
                last_interim_text = transcript
                remaining = transcript[provisional_offset:].strip()

                # Provisional: fire when an interim arrives and we haven't had
                # a final result for PROVISIONAL_TIMEOUT seconds
                if loop.time() - last_final_time >= PROVISIONAL_TIMEOUT and remaining:
                    provisional = _split_at_comma(remaining)
                    chunk_counter += 1
                    chunk_id = f"p{chunk_counter:03d}"
                    logger.info(f"Provisional transcript [{device_id}]: {provisional}")
                    provisional_offset += len(provisional)
                    last_final_time = loop.time()   # reset so next provisional waits another N secs
                    last_interim_words = 0
                    await asyncio.gather(*[
                        process_language_streaming(lang_code, lang_info, provisional, chunk_id, session_id)
                        for lang_code, lang_info in TARGET_LANGUAGES.items()
                    ])
                else:
                    # Normal interim throttle: re-translate every 3 new words
                    word_count = len(remaining.split())
                    if word_count >= last_interim_words + 3:
                        last_interim_words = word_count
                        logger.debug(f"Interim [{device_id}]: {remaining}")
                        await asyncio.gather(*[
                            broadcast_interim_translation(lang_code, lang_info, remaining)
                            for lang_code, lang_info in TARGET_LANGUAGES.items()
                        ])

    stt_future = loop.run_in_executor(None, stt_worker)
    await asyncio.gather(receive_audio(), process_results())
    await stt_future
    logger.info(f"Streaming connection from {device_id} closed")


@app.websocket("/ws/{language_code}")
async def websocket_endpoint(websocket: WebSocket, language_code: str):
    """
    WebSocket endpoint for web clients
    Clients connect to specific language channel
    """
    if language_code not in TARGET_LANGUAGES:
        await websocket.close(code=1003)
        return
    
    await websocket.accept()
    active_connections[language_code].add(websocket)
    
    logger.info(f"Client connected to {language_code} channel. Total: {len(active_connections[language_code])}")
    
    # Send welcome message
    await websocket.send_json({
        "type": "connected",
        "language": language_code,
        "session": current_session_id
    })
    
    try:
        # Keep connection alive
        while True:
            # Receive messages from client (ping/pong)
            data = await websocket.receive_text()
            
            if data == "ping":
                await websocket.send_text("pong")
                
    except WebSocketDisconnect:
        active_connections[language_code].remove(websocket)
        logger.info(f"Client disconnected from {language_code}. Remaining: {len(active_connections[language_code])}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        active_connections[language_code].discard(websocket)


@app.get("/audio/{session_id}/{filename}")
async def serve_audio(session_id: str, filename: str):
    """Serve audio files to web clients"""
    audio_path = SESSIONS_DIR / session_id / "chunks" / filename
    
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    return FileResponse(audio_path)


@app.get("/sessions/{session_id}/manifest")
async def get_session_manifest(session_id: str):
    """Get session manifest with all chunks"""
    manifest_path = SESSIONS_DIR / session_id / "manifest.json"
    
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    
    with open(manifest_path, 'r') as f:
        manifest = json.load(f)
    
    return manifest


@app.post("/session/new")
async def create_new_session():
    """Create a new session (useful for starting fresh)"""
    session_id = create_session()
    return {"session_id": session_id}

# Serve frontend
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/app")
async def serve_frontend():
    return FileResponse(f"{STATIC_DIR}/index.html")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
    )
