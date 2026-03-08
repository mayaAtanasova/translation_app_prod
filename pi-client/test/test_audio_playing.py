import wave
import io
from scipy import signal
import numpy as np
from google.cloud import speech_v1
import os

# Set your credentials path
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'C:/work/translation_app/translation_workflow_test/credentials/video-conference-translator-8794649ea38e.json'

# Read the original WAV file
with wave.open('test.wav', 'rb') as wf:
    original_rate = wf.getframerate()
    n_channels = wf.getnchannels()
    sample_width = wf.getsampwidth()
    audio_data = wf.readframes(wf.getnframes())

print(f"Original: {original_rate}Hz, {n_channels} channels")

# Convert to numpy array
audio_array = np.frombuffer(audio_data, dtype=np.int16)

# Resample to 16kHz if needed
target_rate = 16000
if original_rate != target_rate:
    num_samples = int(len(audio_array) * target_rate / original_rate)
    audio_resampled = signal.resample(audio_array, num_samples)
    audio_resampled = audio_resampled.astype(np.int16)
    print(f"Resampled to {target_rate}Hz")
else:
    audio_resampled = audio_array

# Create WAV at 16kHz
wav_buffer = io.BytesIO()
with wave.open(wav_buffer, 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(16000)
    wf.writeframes(audio_resampled.tobytes())

resampled_data = wav_buffer.getvalue()

# Send to Google STT
client = speech_v1.SpeechClient()
audio = speech_v1.RecognitionAudio(content=resampled_data)
config = speech_v1.RecognitionConfig(
    encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16,
    sample_rate_hertz=16000,
    language_code="en-US",
    enable_automatic_punctuation=True,
)

print("\nTranscribing...")
response = client.recognize(config=config, audio=audio)

if response.results:
    for result in response.results:
        print(f"\nTranscript: {result.alternatives[0].transcript}")
        print(f"Confidence: {result.alternatives[0].confidence:.2%}")
else:
    print("No speech detected!")