"""
Audio Recorder - Captures audio from microphone
"""
import io
import threading
import time
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import SAMPLE_RATE, CHANNELS


class AudioRecorder:
    def __init__(self):
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.is_recording = False
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self._start_time: float = 0
        self._device_id: Optional[int] = None  # None = use system default

        # Real-time audio level for visualization
        self._current_level: float = 0.0
        self._level_lock = threading.Lock()

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """Callback for audio stream"""
        if status:
            print(f"Audio status: {status}")
        if self.is_recording:
            with self._lock:
                self._frames.append(indata.copy())

            # Calculate current audio level (RMS)
            rms = np.sqrt(np.mean(indata ** 2))
            # Normalize to 0-1 range (typical speech is 0.01-0.3)
            normalized = min(1.0, rms * 5)

            with self._level_lock:
                # Smooth the level for better visualization
                self._current_level = self._current_level * 0.3 + normalized * 0.7

    def get_current_level(self) -> float:
        """Get current audio level (0.0 to 1.0) for visualization"""
        with self._level_lock:
            return self._current_level

    def get_recording_duration(self) -> float:
        """Get current recording duration in seconds"""
        if self.is_recording and self._start_time > 0:
            return time.time() - self._start_time
        return 0.0

    def start_recording(self):
        """Start recording audio"""
        with self._lock:
            self._frames = []
            self.is_recording = True
            self._start_time = time.time()

        with self._level_lock:
            self._current_level = 0.0

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=np.float32,
            callback=self._audio_callback,
            blocksize=1024,
            device=self._device_id  # Use selected microphone
        )
        self._stream.start()

    def stop_recording(self) -> tuple[bytes, int]:
        """Stop recording and return audio data as WAV bytes and duration in ms"""
        self.is_recording = False
        duration_ms = int((time.time() - self._start_time) * 1000)

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        with self._lock:
            if not self._frames:
                return b"", 0

            audio_data = np.concatenate(self._frames, axis=0)
            self._frames = []

        # Convert to WAV bytes
        buffer = io.BytesIO()
        sf.write(buffer, audio_data, self.sample_rate, format='WAV', subtype='PCM_16')
        buffer.seek(0)

        return buffer.read(), duration_ms

    def get_available_devices(self) -> list[dict]:
        """Get list of available input devices"""
        devices = sd.query_devices()
        input_devices = []
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                input_devices.append({
                    'id': i,
                    'name': device['name'],
                    'channels': device['max_input_channels'],
                    'default': i == sd.default.device[0]
                })
        return input_devices

    def set_device(self, device_id: Optional[int]):
        """Set the input device"""
        self._device_id = device_id
