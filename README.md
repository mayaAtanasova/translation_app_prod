# Real-Time Conference Translation System

Multi-device translation system for live conferences. Raspberry Pi captures audio, Mac Mini processes translations, web clients view in real-time.

## 🏗️ Architecture

```
┌─────────────────┐
│  Raspberry Pi   │ → Captures audio, performs VAD
│   (Pi Client)   │ → Sends chunks via HTTP POST
└────────┬────────┘
         │
         ↓
┌─────────────────────────────────┐
│       Mac Mini Server           │
│  ┌───────────────────────────┐ │
│  │   FastAPI Backend         │ │ → Receives audio chunks
│  │   - STT (Google Cloud)    │ │ → Transcribes
│  │   - Translation (3x)      │ │ → Translates to 3 languages
│  │   - TTS (3x)              │ │ → Synthesizes speech
│  │   - WebSocket Broadcast   │ │ → Sends to clients
│  └───────────────────────────┘ │
│  ┌───────────────────────────┐ │
│  │   File Storage            │ │ → Saves audio + transcripts
│  │   /data/sessions/...      │ │
│  └───────────────────────────┘ │
└────────┬────────────────────────┘
         │
         ↓
┌─────────────────┐
│   Web Viewers   │ → Select language
│  (3 languages)  │ → See translations
│                 │ → Hear audio
└─────────────────┘
```

## 📦 Components

| Component | Tech | Location | Purpose |
|-----------|------|----------|---------|
| **Backend** | Python FastAPI | Mac Mini | Process audio, translate, broadcast |
| **Pi Client** | Python | Raspberry Pi 5 | Capture audio, send to backend |
| **Frontend** | HTML/JS/WebSocket | Any browser | View translations |

## 🚀 Quick Start (10-Day MVP)

### Day 1-2: Backend Setup

```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set up Google Cloud credentials
export GOOGLE_APPLICATION_CREDENTIALS="./credentials.json"

# Run server
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Day 3-4: Pi Client Setup

```bash
cd pi-client
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy VAD processor from POC
cp ../translation_workflow_test/src/vad_processor.py ./

# Configure backend URL
cp config.json.template config.json
# Edit config.json with Mac Mini IP

# Run client
python main.py
```

### Day 5-6: Frontend

```bash
# Open frontend/index.html in browser
# Or serve via backend at http://MAC_IP:8000/static/index.html
```

## 🎯 MVP Features (Phase 2)

**What We're Building (10 Days):**

- ✅ 1 Raspberry Pi device
- ✅ Mac Mini backend (macOS)
- ✅ 3 output languages (Swedish, Norwegian, German)
- ✅ Real-time web viewer
- ✅ File-based storage (no database)
- ✅ Auto-play audio
- ✅ WebSocket streaming

**What We're Skipping:**

- ❌ Database (use files)
- ❌ User authentication
- ❌ Admin panel
- ❌ Multiple devices
- ❌ Historical playback
- ❌ Device management UI

All Phase 3 additions!

## 📁 Project Structure

```
translation-system/
├── backend/
│   ├── main.py                # FastAPI server
│   ├── requirements.txt
│   ├── README.md
│   └── data/                  # Generated data
│       └── sessions/
│           └── 20260219_140530/
│               ├── config.json
│               ├── manifest.json
│               └── chunks/
│                   ├── 001_original.wav
│                   ├── 001_transcript.txt
│                   ├── 001_sv-SE.mp3
│                   ├── 001_nb-NO.mp3
│                   └── 001_de-DE.mp3
│
├── pi-client/
│   ├── main.py                # Pi audio client
│   ├── vad_processor.py       # Copy from POC
│   ├── config.json            # Backend URL config
│   ├── requirements.txt
│   └── README.md
│
├── frontend/
│   ├── index.html             # Web viewer
│   └── README.md
│
└── docs/
    ├── SETUP.md               # Full setup guide
    ├── TESTING.md             # Testing procedures
    └── TROUBLESHOOTING.md     # Common issues
```

## 🔧 Configuration

### Backend (Mac Mini)

Hardcoded in `backend/main.py`:

```python
TARGET_LANGUAGES = {
    'sv-SE': {'name': 'Swedish', ...},
    'nb-NO': {'name': 'Norwegian', ...},
    'de-DE': {'name': 'German', ...},
}
```

### Pi Client

Edit `pi-client/config.json`:

```json
{
  "backend_url": "http://192.168.1.100:8000",
  "device_id": "pi_device_001",
  "device_index": null
}
```

### Frontend

Edit `frontend/index.html` line 195:

```javascript
const BACKEND_URL = 'ws://192.168.1.100:8000';
```

## 🧪 Testing Checklist

### Pre-Event Testing (Office)

- [ ] Backend starts without errors
- [ ] Pi can connect to backend
- [ ] Audio device detected on Pi
- [ ] VAD detects speech correctly
- [ ] Chunks sent successfully
- [ ] Translations appear on frontend
- [ ] Audio auto-plays
- [ ] All 3 languages work
- [ ] Multiple browsers can connect
- [ ] Reconnection works after disconnect

### Venue Testing

- [ ] Pi and Mac on same network
- [ ] Network has sufficient bandwidth
- [ ] Firewall not blocking port 8000
- [ ] Microphone positioned correctly
- [ ] Audio levels appropriate
- [ ] Latency acceptable (<5 seconds)

## 📊 Performance Targets

| Metric | Target | Notes |
|--------|--------|-------|
| **Total Latency** | < 5 seconds | Speech → Translation display |
| **Pi Processing** | < 200ms | VAD + network upload |
| **Backend** | 2-4 seconds | STT + Translation + TTS |
| **Frontend** | < 100ms | WebSocket delivery |
| **Concurrent Users** | 50+ | Per language channel |

## 🆘 Troubleshooting

### Backend won't start

```bash
# Check port not in use
lsof -i :8000

# Check Google Cloud credentials
echo $GOOGLE_APPLICATION_CREDENTIALS
```

### Pi can't connect

```bash
# Test from Pi
curl http://MAC_IP:8000

# Check network
ping MAC_IP

# Check firewall (on Mac)
sudo lsof -i :8000
```

### No audio on frontend

1. Click anywhere on page (enables auto-play)
2. Check browser console for errors
3. Verify audio URL in network tab

## 🔜 Phase 3 Roadmap

**After successful test:**

1. **Database Migration** (PostgreSQL)
   - Store sessions, chunks, translations
   - Enable historical playback

2. **Admin Panel**
   - Device management
   - Event setup
   - Language configuration
   - User management

3. **Multi-Device Support**
   - Device registration
   - Load balancing
   - Session routing

4. **React Frontend**
   - Professional UI
   - User authentication
   - Session selection
   - Export capabilities

5. **Production Hardening**
   - HTTPS/SSL
   - Backup server
   - Monitoring
   - Analytics

## 📝 License

Proprietary - Internal Use Only

## 👥 Team

- Backend: Python/FastAPI
- Pi Client: Python
- Frontend: HTML/JS (→ React in Phase 3)

## 📞 Support

For issues during development, check:
1. Individual component READMEs
2. `docs/TROUBLESHOOTING.md`
3. Backend logs (console output)
4. Browser console (F12)

---

**Status:** Phase 2 Development  
**Target:** 10 days to working MVP  
**Test Date:** [TBD]
