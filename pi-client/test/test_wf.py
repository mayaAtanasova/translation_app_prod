# Quick test script
import requests
import wave
import io

# Read your 48kHz file
with wave.open("test.wav", 'rb') as wf:
    audio_data = wf.readframes(wf.getnframes())

# Create new 16kHz WAV in memory
wav_buffer = io.BytesIO()
with wave.open(wav_buffer, 'wb') as wf:
    wf.setnchannels(1)  # Mono
    wf.setsampwidth(2)  # 16-bit
    wf.setframerate(16000)  # 16kHz
    wf.writeframes(audio_data)

wav_buffer.seek(0)

# Send to backend
response = requests.post(
    "https://sw-translation.ngrok.app/audio/chunk",
    files={'audio': ('test.wav', wav_buffer, 'audio/wav')},
    data={'chunk_id': 'test_001'}
)
print(response.json())