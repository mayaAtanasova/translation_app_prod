"""
Raspberry Pi Translation Client
Captures audio, performs VAD, sends chunks to backend server
"""

import pyaudio
import wave
import requests
import time
import os
import sys
import json
from datetime import datetime
from pathlib import Path
import threading

try:
    import websocket as ws_client   # websocket-client package
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False

# Add path for imports
sys.path.insert(0, str(Path(__file__).parent))

try:
    from vad_processor import VADProcessor
except ImportError:
    print("Error: vad_processor.py not found. Copy from POC project.")
    sys.exit(1)

# Configuration
class Config:
    # Backend server
    BACKEND_URL = "https://translate.streamworks.no"
    
    # Audio settings
    SAMPLE_RATE = 16000
    CHANNELS = 1
    CHUNK_DURATION_MS = 32
    CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)
    
    # Device
    DEVICE_ID = "pi_device_001"
    DEVICE_INDEX = None  # Auto-detect or set manually
    
    # VAD settings
    VAD_THRESHOLD = 0.5
    SILENCE_THRESHOLD_MS = 150
    MAX_CHUNK_DURATION_MS = 15000
    
    # Local backup (save audio locally in case backend fails)
    ENABLE_LOCAL_BACKUP = True
    LOCAL_BACKUP_DIR = Path("./backup")


class StreamingTranslationClient:
    """
    WebSocket client that streams raw PCM audio frames to the backend
    in real-time as VAD detects speech. The backend pipes these directly
    into Google STT Streaming API for low-latency transcription.
    """

    def __init__(self, backend_url: str, device_id: str):
        self.ws_url = (
            backend_url
            .replace('https://', 'wss://')
            .replace('http://', 'ws://')
        )
        self.device_id = device_id
        self.ws = None
        self.connected = False
        self._connect()

    def _connect(self):
        url = f"{self.ws_url}/ws/audio/{self.device_id}"
        self.ws = ws_client.WebSocketApp(
            url,
            on_open=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        t = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"reconnect": 5},
            daemon=True,
        )
        t.start()

    def _on_open(self, ws):
        self.connected = True
        print("✓ Streaming WebSocket connected to backend")

    def _on_close(self, ws, code, msg):
        self.connected = False
        print(f"WebSocket closed (code={code}), reconnecting in 5s...")

    def _on_error(self, ws, error):
        print(f"WebSocket error: {error}")

    def send_audio(self, audio_bytes: bytes):
        if self.ws and self.connected:
            try:
                self.ws.send(audio_bytes, ws_client.ABNF.OPCODE_BINARY)
            except Exception as e:
                print(f"Error sending audio frame: {e}")
                self.connected = False

    def close(self):
        if self.ws:
            self.ws.close()


class AudioCapture:
    """Handle audio input from microphone"""
    
    def __init__(self, device_index=None):
        self.device_index = device_index
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.actual_sample_rate = Config.SAMPLE_RATE
        self.capture_chunk_size = Config.CHUNK_SIZE
        
    def list_devices(self):
        """List available audio devices"""
        print("\n" + "="*60)
        print("Available Audio Devices:")
        print("="*60)
        
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                print(f"[{i}] {info['name']}")
                print(f"    Channels: {info['maxInputChannels']}, Rate: {info['defaultSampleRate']}")
        
        print("="*60)
    
    def select_device(self):
        """Interactive device selection"""
        self.list_devices()
        
        while True:
            try:
                choice = input("\nSelect device number (or press Enter for default): ").strip()
                if not choice:
                    self.device_index = None
                    print("Using default device")
                    break
                
                idx = int(choice)
                info = self.audio.get_device_info_by_index(idx)
                if info['maxInputChannels'] > 0:
                    self.device_index = idx
                    print(f"Selected: {info['name']}")
                    break
                else:
                    print("Device does not support input")
            except (ValueError, OSError):
                print("Invalid selection")
    
    def start_stream(self):
        """Start audio stream, falling back to device native rate if 16kHz is unsupported."""
        # Determine capture rate: prefer 16kHz, fall back to device default
        device_info = (
            self.audio.get_device_info_by_index(self.device_index)
            if self.device_index is not None
            else self.audio.get_default_input_device_info()
        )
        native_rate = int(device_info['defaultSampleRate'])
        candidate_rates = [Config.SAMPLE_RATE, native_rate]

        for rate in candidate_rates:
            try:
                chunk_size = int(rate * Config.CHUNK_DURATION_MS / 1000)
                self.stream = self.audio.open(
                    format=pyaudio.paInt16,
                    channels=Config.CHANNELS,
                    rate=rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=chunk_size,
                )
                self.actual_sample_rate = rate
                self.capture_chunk_size = chunk_size
                if rate != Config.SAMPLE_RATE:
                    print(f"✓ Audio stream started at {rate} Hz (will resample to {Config.SAMPLE_RATE} Hz)")
                else:
                    print(f"✓ Audio stream started at {rate} Hz")
                return True
            except Exception as e:
                if rate == candidate_rates[-1]:
                    print(f"✗ Failed to start audio stream: {e}")
                    return False

    def read_chunk(self):
        """Read one chunk of audio, resampling to 16kHz if the device runs at a different rate."""
        if not self.stream:
            return None

        try:
            raw = self.stream.read(self.capture_chunk_size, exception_on_overflow=False)
        except Exception as e:
            print(f"Error reading audio: {e}")
            return None

        if self.actual_sample_rate == Config.SAMPLE_RATE:
            return raw

        # Resample from device rate → 16kHz using numpy linear interpolation
        import numpy as np
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        target_len = int(len(pcm) * Config.SAMPLE_RATE / self.actual_sample_rate)
        resampled = np.interp(
            np.linspace(0, len(pcm) - 1, target_len),
            np.arange(len(pcm)),
            pcm,
        ).astype(np.int16)
        return resampled.tobytes()
    
    def cleanup(self):
        """Stop and close stream"""
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        
        self.audio.terminate()


class TranslationClient:
    """Main client application"""
    
    def __init__(self):
        self.backend_url = Config.BACKEND_URL
        self.audio_capture = None
        self.vad = VADProcessor(
            threshold=Config.VAD_THRESHOLD,
        )
        self.chunk_counter = 0
        self.running = False
        self.streaming_client = None

        # Create backup directory if enabled
        if Config.ENABLE_LOCAL_BACKUP:
            Config.LOCAL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        print("\n" + "🎤" * 30)
        print("TRANSLATION CLIENT - RASPBERRY PI")
        print("🎤" * 30)
        print(f"Backend: {self.backend_url}")
        print(f"Device ID: {Config.DEVICE_ID}")
        if WEBSOCKET_AVAILABLE:
            print("Mode:    streaming WebSocket")
        else:
            print("Mode:    HTTP POST (install websocket-client for streaming)")
        print("=" * 60 + "\n")
    
    def test_backend_connection(self):
        """Test connection to backend server"""
        print("Testing backend connection...")
        
        try:
            response = requests.get(f"{self.backend_url}/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                print(f"✓ Backend connected: {data}")
                return True
            else:
                print(f"✗ Backend returned status {response.status_code}")
                return False
        except requests.exceptions.ConnectionError:
            print(f"✗ Cannot connect to backend at {self.backend_url}")
            print("  Check:")
            print("  1. Backend is running on Mac Mini")
            print("  2. IP address is correct")
            print("  3. Pi and Mac Mini are on same network")
            print("  4. Port 8000 is not blocked by firewall")
            return False
        except Exception as e:
            print(f"✗ Error: {e}")
            return False
    
    def send_audio_chunk(self, audio_data):
        """Send audio chunk to backend"""
        self.chunk_counter += 1
        chunk_id = f"{self.chunk_counter:03d}"
        
        # Save locally if backup enabled
        if Config.ENABLE_LOCAL_BACKUP:
            backup_path = Config.LOCAL_BACKUP_DIR / f"{chunk_id}.wav"
            self._save_wav(audio_data, backup_path)
        
        # Send to backend
        try:
            # Create WAV file in memory
            import io
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wf:
                wf.setnchannels(Config.CHANNELS)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(Config.SAMPLE_RATE)
                wf.writeframes(audio_data)
            
            wav_buffer.seek(0)
            
            # Send POST request
            files = {'audio': ('chunk.wav', wav_buffer, 'audio/wav')}
            data = {'chunk_id': chunk_id}
            
            response = requests.post(
                f"{self.backend_url}/audio/chunk",
                files=files,
                data=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result['status'] == 'success':
                    print(f"✓ Chunk {chunk_id} processed: {result.get('transcript', '')[:50]}...")
                    return True
                else:
                    print(f"⚠ Chunk {chunk_id}: {result['status']}")
                    return False
            else:
                print(f"✗ Backend error: {response.status_code}")
                return False
                
        except requests.exceptions.Timeout:
            print(f"✗ Chunk {chunk_id} timeout (backend too slow?)")
            return False
        except requests.exceptions.ConnectionError:
            print(f"✗ Lost connection to backend")
            return False
        except Exception as e:
            print(f"✗ Error sending chunk: {e}")
            return False
    
    def _save_wav(self, audio_data, filepath):
        """Save raw audio as WAV file"""
        with wave.open(str(filepath), 'wb') as wf:
            wf.setnchannels(Config.CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(Config.SAMPLE_RATE)
            wf.writeframes(audio_data)
    
    def run(self):
        """Main loop - capture audio and send to backend"""
        # Test backend
        if not self.test_backend_connection():
            print("\n⚠️  Cannot connect to backend. Continue anyway? (y/n): ", end="")
            if input().strip().lower() != 'y':
                return
            print("⚠️  Running in offline mode - audio will be saved locally only\n")
        
        # Setup audio
        self.audio_capture = AudioCapture(device_index=Config.DEVICE_INDEX)
        
        if Config.DEVICE_INDEX is None:
            self.audio_capture.select_device()
        
        if not self.audio_capture.start_stream():
            print("✗ Failed to start audio capture")
            return
        
        print("\n✓ Ready to capture audio")
        print("Speak into the microphone. Press Ctrl+C to stop.\n")

        # Start streaming WebSocket connection if available
        if WEBSOCKET_AVAILABLE:
            self.streaming_client = StreamingTranslationClient(
                backend_url=self.backend_url,
                device_id=Config.DEVICE_ID,
            )
            # Give the WebSocket a moment to connect before audio starts
            time.sleep(1.5)

        self.running = True

        # Streaming state: VAD triggers the start of streaming; a long silence
        # ends it. While streaming, ALL audio (speech + silence) is forwarded
        # so Google STT gets a continuous signal and can detect sentence
        # boundaries itself via natural pauses in the audio.
        STREAM_END_SILENCE_MS = 5000   # ms of silence before pausing the stream
        streaming_active = False
        stream_silence_ms = 0.0
        silence_count = 0

        try:
            while self.running:
                chunk = self.audio_capture.read_chunk()
                if not chunk:
                    break

                speech_data, is_complete = self.vad.process_chunk(chunk)
                chunk_ms = len(chunk) / 2 / Config.SAMPLE_RATE * 1000

                if self.streaming_client and self.streaming_client.connected:
                    # ── Streaming mode ──────────────────────────────────
                    if self.vad.triggered:
                        # Speech detected — start streaming if not already active
                        if not streaming_active:
                            print("\n▶ streaming", flush=True)
                            streaming_active = True
                        stream_silence_ms = 0.0
                        silence_count = 0
                        self.streaming_client.send_audio(chunk)
                        print("🎤", end="", flush=True)

                    elif streaming_active:
                        # Silence while stream is active — keep sending so
                        # Google sees the pause and can finalize sentences
                        stream_silence_ms += chunk_ms
                        self.streaming_client.send_audio(chunk)
                        silence_count += 1
                        if silence_count % 10 == 0:
                            print(".", end="", flush=True)

                        if stream_silence_ms >= STREAM_END_SILENCE_MS:
                            streaming_active = False
                            stream_silence_ms = 0.0
                            print("\n⏸ stream paused (long silence)", flush=True)

                    else:
                        # Idle — waiting for speech to start
                        silence_count += 1
                        if silence_count % 10 == 0:
                            print(".", end="", flush=True)

                else:
                    # ── HTTP POST fallback (original behaviour) ──────────
                    if is_complete and speech_data:
                        print()
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Sending chunk (HTTP)...")
                        threading.Thread(
                            target=self.send_audio_chunk,
                            args=(speech_data,),
                            daemon=True,
                        ).start()
                    elif self.vad.triggered:
                        print("🎤", end="", flush=True)
                        silence_count = 0
                    else:
                        silence_count += 1
                        if silence_count % 10 == 0:
                            print(".", end="", flush=True)
        
        except KeyboardInterrupt:
            print("\n\nStopping...")
        
        finally:
            self.stop()
    
    def stop(self):
        """Cleanup and stop"""
        self.running = False

        if self.streaming_client:
            self.streaming_client.close()

        if self.audio_capture:
            self.audio_capture.cleanup()
        
        print("\n" + "="*60)
        print(f"Session complete!")
        if self.streaming_client:
            print("Mode: streaming WebSocket (see backend logs for chunk count)")
        else:
            print(f"Chunks sent: {self.chunk_counter}")
        if Config.ENABLE_LOCAL_BACKUP:
            print(f"Local backup: {Config.LOCAL_BACKUP_DIR}")
        print("="*60 + "\n")


def main():
    """Entry point"""
    # Check for config file (optional)
    config_file = Path("config.json")
    if config_file.exists():
        print("Loading configuration from config.json...")
        with open(config_file) as f:
            config = json.load(f)
            Config.BACKEND_URL = config.get('backend_url', Config.BACKEND_URL)
            Config.DEVICE_ID = config.get('device_id', Config.DEVICE_ID)
            Config.DEVICE_INDEX = config.get('device_index', Config.DEVICE_INDEX)
    
    # Run client
    client = TranslationClient()
    client.run()


if __name__ == "__main__":
    main()
