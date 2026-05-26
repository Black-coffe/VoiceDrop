"""
Audio Recorder - Captures audio from microphone
"""
import io
import logging
import threading
import time
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import SAMPLE_RATE, CHANNELS

# When one physical mic is exposed under several host APIs, prefer them in this
# order. MME first = same device VoiceDrop has always opened, and most stable.
_HOSTAPI_PRIORITY = {
    'MME': 0,
    'Windows WASAPI': 1,
    'Windows DirectSound': 2,
    'Windows WDM-KS': 3,
}

# Open at most twice: first try uses the cached device list; if it fails, wait
# a short settle delay and retry the SAME device once. We deliberately do NOT
# call sd._terminate()/sd._initialize() between attempts — issue #516 / #47:
# refreshing PortAudio's cache mid-process is documented to lose devices and
# can crash the interpreter. Production log confirms it: FOX disappeared after
# the refresh and never came back without an app restart.
_OPEN_MAX_ATTEMPTS = 2
_OPEN_RETRY_SETTLE_SEC = 0.18


class AudioRecorder:
    def __init__(self):
        # Fallback rate used only if querying the device's native rate fails.
        # The actual capture rate is set per-recording in start_recording().
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.is_recording = False
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self._start_time: float = 0
        # The mic is identified BY NAME (Windows device indices are unstable).
        # _device_id is only a legacy hint used when no name is configured.
        self._device_name: Optional[str] = None
        self._device_hostapi: Optional[str] = None
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

    def _query_native_samplerate(self, device_index: Optional[int]) -> int:
        """Return the device's native samplerate; fall back to SAMPLE_RATE on failure.

        We open at the device's native rate so Windows shared-mode's resampler
        is never in the path — that resampler is the leading cause of the
        intermittent PaErrorCode -9999 'device ID out of range' on USB mics
        whose native format (e.g. FOX = 44100 Hz, some interfaces 48000/88200)
        doesn't match VoiceDrop's STT-target 16 kHz. ElevenLabs Scribe accepts
        any common samplerate, so we just upload the WAV at the captured rate.
        """
        try:
            info = sd.query_devices(device_index) if device_index is not None else sd.query_devices(kind='input')
            sr = int(round(float(info.get('default_samplerate') or 0)))
            if sr >= 8000:
                return sr
        except Exception as e:
            logging.warning(f"Could not query device samplerate, using fallback {SAMPLE_RATE}: {e}")
        return SAMPLE_RATE

    def start_recording(self):
        """Start recording audio from the configured mic (resolved BY NAME).

        Raises RuntimeError if a specific microphone was configured but is not
        currently present — we never silently fall back to the system default.

        Open path: query the device's native samplerate and open at that rate.
        On failure, wait briefly and retry the SAME device once. We do NOT
        rebuild PortAudio's device cache here — see _OPEN_MAX_ATTEMPTS note.
        """
        last_err: Optional[Exception] = None
        for attempt in range(_OPEN_MAX_ATTEMPTS):
            if attempt > 0:
                # Brief settle so the device/driver has a moment to recover
                # (USB wakeup, MME WaveIn driver re-arm). No cache refresh.
                time.sleep(_OPEN_RETRY_SETTLE_SEC)

            try:
                device_index = self._resolve_device_for_capture()
            except RuntimeError as e:
                # Configured mic genuinely not in the current device list —
                # no point retrying without external state changing.
                raise

            capture_sr = self._query_native_samplerate(device_index)

            with self._lock:
                self._frames = []
                self.is_recording = True
                self._start_time = time.time()
                self.sample_rate = capture_sr
            with self._level_lock:
                self._current_level = 0.0

            try:
                self._stream = sd.InputStream(
                    samplerate=capture_sr,
                    channels=self.channels,
                    dtype=np.float32,
                    callback=self._audio_callback,
                    blocksize=1024,
                    device=device_index
                )
                self._stream.start()
                if attempt == 0:
                    logging.info(f"Recording at native {capture_sr} Hz")
                else:
                    logging.info(f"Recording at native {capture_sr} Hz (recovered on attempt {attempt + 1})")
                return  # success
            except Exception as e:
                # Roll back so a failed open can't wedge the recording state.
                with self._lock:
                    self.is_recording = False
                self._stream = None
                last_err = e
                logging.warning(
                    f"InputStream open failed (attempt {attempt + 1}): {e}"
                )

        # All attempts exhausted — surface the last error to the caller.
        assert last_err is not None
        raise last_err

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
        """Get list of available input devices (with host API name)."""
        devices = sd.query_devices()
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []
        input_devices = []
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                try:
                    hostapi = hostapis[device['hostapi']]['name']
                except Exception:
                    hostapi = ''
                input_devices.append({
                    'id': i,
                    'name': device['name'],
                    'hostapi': hostapi,
                    'channels': device['max_input_channels'],
                    'default': i == sd.default.device[0]
                })
        return input_devices

    def set_device(self, device_id: Optional[int] = None, device_name: Optional[str] = None,
                   device_hostapi: Optional[str] = None):
        """Configure the input device.

        Prefer device_name — it survives the index reshuffling that happens on
        Windows when devices are added/removed. device_id is kept only as a
        legacy hint for when no name is available.
        """
        self._device_id = device_id
        self._device_name = device_name
        self._device_hostapi = device_hostapi

    def resolve_device_index(self) -> tuple[Optional[int], str]:
        """Resolve the configured microphone to a CURRENT device index, BY NAME.

        Returns (index, detail). index is None when nothing usable was resolved.
        Never silently substitutes the system default for a configured mic.
        """
        name = self._device_name
        if not name:
            if self._device_id is not None:
                return self._device_id, f"index #{self._device_id} (no name configured)"
            return None, "system default (no microphone configured)"

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            return None, f"query_devices failed: {e}"

        def hostapi_name(dev) -> str:
            try:
                return hostapis[dev['hostapi']]['name']
            except Exception:
                return '?'

        # Exact name match among input devices; fall back to prefix match to
        # tolerate host-API suffixes / MME's 31-char name truncation.
        matches = [i for i, d in enumerate(devices)
                   if d['max_input_channels'] > 0 and d['name'] == name]
        if not matches:
            matches = [i for i, d in enumerate(devices)
                       if d['max_input_channels'] > 0
                       and (d['name'].startswith(name) or name.startswith(d['name']))]

        if not matches:
            return None, f"'{name}' not found among current input devices"

        # Prefer the exact host API the user picked; else the priority order.
        def rank(i: int) -> tuple:
            ha = hostapi_name(devices[i])
            picked = 0 if (self._device_hostapi and ha == self._device_hostapi) else 1
            return (picked, _HOSTAPI_PRIORITY.get(ha, 99), i)

        matches.sort(key=rank)
        idx = matches[0]
        return idx, f"{devices[idx]['name']} [#{idx}, {hostapi_name(devices[idx])}]"

    def _resolve_device_for_capture(self) -> Optional[int]:
        """Return the device index to open, or raise if a configured mic is gone."""
        idx, detail = self.resolve_device_index()
        if idx is None and self._device_name:
            raise RuntimeError(
                f"Микрофон «{self._device_name}» не найден. "
                f"Проверьте подключение или выберите микрофон в настройках."
            )
        logging.info(f"Opening microphone: {detail}")
        return idx
