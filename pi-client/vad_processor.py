"""
Voice Activity Detection (VAD) processor
Uses Silero VAD for modern, reliable speech detection
"""

import torch
import numpy as np
import collections
import sys
import os

# Add parent directory to path to import config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings


class VADProcessor:
    """
    Voice Activity Detection using Silero VAD
    Detects speech vs silence and manages buffering
    """
    
    def __init__(self, threshold=0.5):
        """
        Initialize VAD processor
        
        Args:
            threshold: Speech probability threshold (0.0-1.0)
        """
        # Load Silero VAD model
        print("Loading Silero VAD model...")
        self.model, self.utils = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=False
        )
        
        # Get utility functions
        (self.get_speech_timestamps,
         self.save_audio,
         self.read_audio,
         self.VADIterator,
         self.collect_chunks) = self.utils
        
        self.sample_rate = settings.SAMPLE_RATE
        self.threshold = threshold
        
        # Buffering settings
        self.speech_pad_ms = settings.SPEECH_PADDING_MS
        self.silence_threshold_ms = settings.SILENCE_THRESHOLD
        
        # Max chunk duration - force split if speaker doesn't pause
        self.max_chunk_duration_ms = settings.MAX_CHUNK_DURATION_MS
        self.max_chunk_bytes = int(self.sample_rate * self.max_chunk_duration_ms / 1000) * 2
        
        # State
        self.triggered = False
        self.speech_buffer = []
        self.silence_duration = 0
        self.speech_duration = 0  # Track how long current chunk has been
        
        print(f"✓ Silero VAD initialized (threshold={threshold}, "
              f"silence_threshold={self.silence_threshold_ms}ms, "
              f"max_chunk={self.max_chunk_duration_ms}ms)")
    
    def is_speech(self, audio_chunk):
        """
        Check if audio chunk contains speech
        
        Args:
            audio_chunk: Raw audio data (bytes, 16-bit PCM)
        
        Returns:
            float: Speech probability (0.0-1.0), or 0.0 if chunk too small
        """
        # Minimum chunk size for Silero VAD = 512 samples = 1024 bytes
        min_bytes = 512 * 2  # 512 samples * 2 bytes per sample
        if len(audio_chunk) < min_bytes:
            return 0.0  # Treat as silence, don't crash
        
        # Convert bytes to numpy array
        audio_int16 = np.frombuffer(audio_chunk, dtype=np.int16)
        
        # Convert to float32 normalized to [-1, 1]
        audio_float32 = audio_int16.astype(np.float32) / 32768.0
        
        # Convert to torch tensor
        audio_tensor = torch.from_numpy(audio_float32)
        
        # Get speech probability
        try:
            with torch.no_grad():
                speech_prob = self.model(audio_tensor, self.sample_rate).item()
            return speech_prob
        except Exception as e:
            return 0.0  # Treat as silence on any error
    
    def process_chunk(self, audio_chunk):
        """
        Process a single audio chunk with simple VAD logic
        
        Args:
            audio_chunk: Raw audio bytes (16-bit PCM)
        
        Returns:
            tuple: (speech_data, is_complete) 
                - speech_data: Buffered speech or None
                - is_complete: True when speech segment ends
        """
        speech_prob = self.is_speech(audio_chunk)
        is_speech = speech_prob > self.threshold
        
        chunk_duration_ms = len(audio_chunk) / 2 / self.sample_rate * 1000
        
        if is_speech:
            self.speech_buffer.append(audio_chunk)
            self.silence_duration = 0
            self.triggered = True
            self.speech_duration += chunk_duration_ms
            
            # Force split if max chunk duration exceeded
            if self.speech_duration >= self.max_chunk_duration_ms:
                speech_data = b''.join(self.speech_buffer)
                self.speech_buffer = []
                self.silence_duration = 0
                self.speech_duration = 0
                # Keep triggered = True (speaker is still talking)
                return speech_data, True
            
            return None, False
        else:
            if self.triggered and len(self.speech_buffer) > 0:
                # Add a bit of silence to the end
                self.speech_buffer.append(audio_chunk)
                self.silence_duration += chunk_duration_ms
                self.speech_duration += chunk_duration_ms
                
                # Check if we've had enough silence to end the segment
                if self.silence_duration >= self.silence_threshold_ms:
                    # Return complete speech segment
                    speech_data = b''.join(self.speech_buffer)
                    self.speech_buffer = []
                    self.silence_duration = 0
                    self.speech_duration = 0
                    self.triggered = False
                    return speech_data, True
            
            return None, False
    
    def reset(self):
        """Reset VAD state"""
        self.speech_buffer = []
        self.silence_duration = 0
        self.speech_duration = 0
        self.triggered = False

    def flush(self):
        """
        Flush any remaining audio in the buffer
        Call this at end of file to process the last segment
        
        Returns:
            tuple: (speech_data, True) or (None, False)
        """
        if self.speech_buffer:
            speech_data = b''.join(self.speech_buffer)
            self.reset()
            return speech_data, True
        return None, False


class SimpleVAD:
    """
    Simplified VAD using energy-based detection
    Fallback option if Silero is too heavy
    """
    
    def __init__(self, threshold=500):
        """Initialize simple energy-based VAD"""
        self.threshold = threshold
        self.speech_buffer = []
        self.silence_frames = 0
        self.max_silence_frames = int(settings.SILENCE_THRESHOLD / settings.CHUNK_DURATION_MS)
        
        print(f"✓ Simple energy-based VAD initialized (threshold={threshold})")
    
    def is_speech(self, audio_chunk):
        """
        Check if audio chunk contains speech based on energy
        
        Args:
            audio_chunk: Raw audio bytes
        
        Returns:
            bool: True if speech detected
        """
        # Convert to numpy array
        audio_int16 = np.frombuffer(audio_chunk, dtype=np.int16)
        
        # Calculate RMS energy
        energy = np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2))
        
        return energy > self.threshold
    
    def process_chunk(self, audio_chunk):
        """
        Process a single audio chunk
        
        Args:
            audio_chunk: Raw audio bytes
        
        Returns:
            tuple: (speech_data, is_complete) or (None, False)
        """
        is_speech = self.is_speech(audio_chunk)
        
        if is_speech:
            self.speech_buffer.append(audio_chunk)
            self.silence_frames = 0
        else:
            if len(self.speech_buffer) > 0:
                self.silence_frames += 1
                
                if self.silence_frames >= self.max_silence_frames:
                    # End of speech segment
                    speech_data = b''.join(self.speech_buffer)
                    self.speech_buffer = []
                    self.silence_frames = 0
                    return speech_data, True
        
        return None, False


def test_vad():
    """Test VAD functionality"""
    print("\n" + "="*60)
    print("Testing Voice Activity Detection")
    print("="*60)
    
    try:
        vad = VADProcessor(threshold=0.5)
        print("✓ Silero VAD loaded successfully")
        
        # Create test audio
        import struct
        sample_rate = settings.SAMPLE_RATE
        chunk_size = int(sample_rate * 0.03)  # 30ms
        
        # Silent chunk
        silent_chunk = struct.pack(f'{chunk_size}h', *([0] * chunk_size))
        
        # Noisy chunk
        import random
        noisy_chunk = struct.pack(f'{chunk_size}h', 
                                 *([random.randint(-5000, 5000) for _ in range(chunk_size)]))
        
        print(f"\nTesting silent chunk: {vad.is_speech(silent_chunk):.3f}")
        print(f"Testing noisy chunk: {vad.is_speech(noisy_chunk):.3f}")
        
    except Exception as e:
        print(f"Error loading Silero VAD: {e}")
        print("Falling back to SimpleVAD...")
        vad = SimpleVAD()


if __name__ == "__main__":
    test_vad()
