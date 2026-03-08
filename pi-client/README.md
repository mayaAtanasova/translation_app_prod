# Raspberry Pi Translation Client

Audio capture client for Raspberry Pi 5. Captures audio, performs VAD, sends chunks to backend.

## Setup on Raspberry Pi

### Prerequisites

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.12 (if not already installed)
sudo apt install python3.12 python3.12-venv python3-pip -y

# Install PortAudio (for PyAudio)
sudo apt install portaudio19-dev -y
```

### Installation

```bash
cd ~/translation-system/pi-client

# Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Copy VAD Processor

Copy `vad_processor.py` from the POC project:

```bash
cp ~/translation_workflow_test/src/vad_processor.py ./
```

### Configuration

1. Copy config template:
```bash
cp config.json.template config.json
```

2. Edit `config.json`:
```json
{
  "backend_url": "http://192.168.1.100:8000",  // Your Mac Mini IP
  "device_id": "pi_device_001",
  "device_index": null  // null = auto-select, or specific number
}
```

Find Mac Mini IP from Mac:
```bash
ifconfig | grep "inet " | grep -v 127.0.0.1
```

### Running

```bash
source venv/bin/activate
python main.py
```

You'll be prompted to select an audio device if not configured.

## Testing

### Test Backend Connection

```bash
# From Pi, test if you can reach backend
curl http://MAC_MINI_IP:8000

# Should return:
# {"status":"running","session":"...","languages":["sv-SE","nb-NO","de-DE"]}
```

### Test Audio Input

```bash
# List audio devices
python -c "import pyaudio; p=pyaudio.PyAudio(); [print(f'{i}: {p.get_device_info_by_index(i)[\"name\"]}') for i in range(p.get_device_count())]; p.terminate()"
```

## Usage

1. **Start backend** on Mac Mini first
2. **Run Pi client**:
   ```bash
   python main.py
   ```
3. **Select audio device** (if prompted)
4. **Speak** into microphone
5. Watch console for status messages

## Visual Feedback

- `🎤` - Currently speaking (voice detected)
- `.` - Silence (waiting for speech)
- `✓ Chunk XXX processed` - Successfully sent to backend

## Troubleshooting

### Cannot connect to backend

**Check 1: Is backend running?**
```bash
curl http://MAC_MINI_IP:8000
```

**Check 2: Are Pi and Mac on same network?**
```bash
ping MAC_MINI_IP
```

**Check 3: Is firewall blocking port 8000?**
On Mac Mini:
```bash
sudo lsof -i :8000  # Should show uvicorn process
```

### No audio input

**Check 1: Is USB mic connected?**
```bash
lsusb  # Should show your microphone
```

**Check 2: Test microphone**
```bash
arecord -l  # List capture devices
arecord -d 3 test.wav  # Record 3 seconds
aplay test.wav  # Play back
```

**Check 3: Set correct device in config.json**

### PyAudio installation fails

```bash
# Make sure PortAudio is installed
sudo apt install portaudio19-dev

# Try installing again
pip install --no-cache-dir pyaudio
```

### VAD/Torch errors

```bash
# Torch might take a while to download on Pi
# Be patient during first run (downloads Silero VAD model)

# If installation fails, try:
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

## Local Backup

Audio is automatically saved to `./backup/` folder in case backend connection fails.

To disable:
```python
# In main.py
Config.ENABLE_LOCAL_BACKUP = False
```

## Performance

**Expected on Raspberry Pi 5:**
- VAD processing: ~5ms per chunk
- Network upload: ~50-100ms per chunk
- Total latency: Minimal (< 200ms added by Pi)

Main latency comes from backend processing (2-4 seconds).

## Next Steps

- [ ] Test with USB microphone
- [ ] Test at venue network
- [ ] Optimize VAD settings
- [ ] Add reconnection logic
- [ ] Add status LED indicators
- [ ] Remote configuration via backend API
