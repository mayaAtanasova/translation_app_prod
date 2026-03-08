# Translation System Backend

FastAPI server for real-time conference translation system.

## Features

- Receives audio chunks from Raspberry Pi devices
- Processes via Google Cloud APIs (STT → Translation → TTS)
- Broadcasts translations to web clients via WebSocket
- File-based storage (no database for MVP)
- Supports 3 languages: Swedish, Norwegian, German

## Setup on Mac Mini

### Prerequisites

```bash
# Install Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python 3.12
brew install python@3.12
```

### Installation

```bash
cd ~/translation-system/backend

# Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Google Cloud Setup

1. Place your `service-account-credentials.json` in the backend directory
2. Set environment variable:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$PWD/service-account-credentials.json"
```

Or add to `~/.zshrc` or `~/.bash_profile`:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/translation-system/backend/service-account-credentials.json"
```

### Running the Server

```bash
# Development mode (with auto-reload)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production mode
uvicorn main:app --host 0.0.0.0 --port 8000
```

Server will be accessible at:
- Local: `http://localhost:8000`
- Network: `http://YOUR_MAC_IP:8000`

Find your Mac IP:
```bash
ifconfig | grep "inet " | grep -v 127.0.0.1
```

### Testing

```bash
# Check server is running
curl http://localhost:8000

# Should return:
# {"status":"running","session":"20260219_140530","languages":["sv-SE","nb-NO","de-DE"]}
```

## API Endpoints

### REST Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| POST | `/audio/chunk` | Receive audio chunk from Pi |
| GET | `/audio/{session}/{file}` | Serve audio file |
| GET | `/sessions/{id}/manifest` | Get session data |
| POST | `/session/new` | Create new session |

### WebSocket Endpoints

| Endpoint | Description |
|----------|-------------|
| `/ws/sv-SE` | Swedish translation stream |
| `/ws/nb-NO` | Norwegian translation stream |
| `/ws/de-DE` | German translation stream |

## File Structure

```
data/
└── sessions/
    └── 20260219_140530/          # Session timestamp
        ├── config.json            # Session metadata
        ├── manifest.json          # Index of all chunks
        └── chunks/
            ├── 001_original.wav   # Original audio
            ├── 001_transcript.txt # Transcript
            ├── 001_sv-SE.mp3      # Swedish translation
            ├── 001_nb-NO.mp3      # Norwegian translation
            ├── 001_de-DE.mp3      # German translation
            └── ...
```

## Configuration

Hardcoded for MVP (edit `main.py` to change):

```python
# Target languages
TARGET_LANGUAGES = {
    'sv-SE': {'name': 'Swedish', 'translate_code': 'sv', 'tts_code': 'sv-SE'},
    'nb-NO': {'name': 'Norwegian', 'translate_code': 'no', 'tts_code': 'nb-NO'},
    'de-DE': {'name': 'German', 'translate_code': 'de', 'tts_code': 'de-DE'},
}

# Source language
source_language = "en-US"

# Device ID
device_id = "pi_device_001"
```

## Troubleshooting

### Port already in use
```bash
# Find process using port 8000
lsof -i :8000

# Kill it
kill -9 <PID>
```

### Google Cloud errors
```bash
# Verify credentials
gcloud auth application-default login

# Or check environment variable
echo $GOOGLE_APPLICATION_CREDENTIALS
```

### WebSocket connection issues
- Check firewall settings
- Ensure port 8000 is accessible
- Try: `telnet YOUR_MAC_IP 8000`

## Logs

Logs are printed to console with INFO level. Watch for:
- `Backend starting up...`
- `Created session: XXXXX`
- `Receiving chunk XXX`
- `Client connected to sv-SE channel`

## Performance

**Expected latency per chunk:**
- Upload: ~100ms
- STT: ~500ms-1s
- Translation (3x): ~500ms-1s
- TTS (3x): ~1-2s
- Total: **2-4 seconds**

## Next Steps (Phase 3)

- [ ] Add PostgreSQL database
- [ ] Admin panel for device management
- [ ] User authentication
- [ ] Session management UI
- [ ] Historical playback
- [ ] Load balancing
- [ ] HTTPS/SSL
- [ ] Monitoring/analytics
