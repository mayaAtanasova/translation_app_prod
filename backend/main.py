"""
Translation System Backend
FastAPI server for real-time conference translation
Handles audio chunks from Raspberry Pi devices and serves translations to web clients
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
from typing import Dict, Set
import logging

# Google Cloud imports
from google.cloud import speech_v1
from google.cloud import translate_v2 as translate
from google.cloud import texttospeech

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Translation System Backend", version="1.0.0")

# CORS middleware for web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For MVP, allow all. Restrict in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DATA_DIR = Path("./data")
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

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
    """Health check endpoint"""
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
        
        # Step 2 & 3: Translate and synthesize for each target language
        chunk_data = {
            "chunk_id": chunk_id,
            "timestamp": datetime.now().isoformat(),
            "transcript": transcript,
            "confidence": confidence,
            "translations": {}
        }
        
        for lang_code, lang_info in TARGET_LANGUAGES.items():
            logger.info(f"Processing {lang_info['name']}...")
            
            # Translate
            translation_result = translate_client.translate(
                transcript,
                target_language=lang_info['translate_code'],
                source_language='en'
            )
            translated_text = translation_result['translatedText']
            
            logger.info(f"{lang_info['name']}: {translated_text}")
            
            # Text-to-Speech
            synthesis_input = texttospeech.SynthesisInput(text=translated_text)
            voice = texttospeech.VoiceSelectionParams(
                language_code=lang_info['tts_code'],
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3
            )
            
            tts_response = tts_client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )
            
            # Save translated audio
            audio_path = chunks_dir / f"{chunk_id}_{lang_code}.mp3"
            with open(audio_path, 'wb') as f:
                f.write(tts_response.audio_content)
            
            # Store translation data
            chunk_data['translations'][lang_code] = {
                "text": translated_text,
                "audio_url": f"/audio/{session_id}/{chunk_id}_{lang_code}.mp3"
            }
            
            # Broadcast to connected clients
            await broadcast_translation(lang_code, {
                "type": "translation",
                "chunk_id": chunk_id,
                "timestamp": chunk_data['timestamp'],
                "original": transcript,
                "translation": translated_text,
                "audio_url": chunk_data['translations'][lang_code]['audio_url'],
                "language": lang_code
            })
        
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


if __name__ == "__main__":
    # Run with: python main.py
    # Or: uvicorn main:app --reload --host 0.0.0.0 --port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
