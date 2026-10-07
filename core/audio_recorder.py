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

# Open with several attempts. The mic can fail to open right after a USB
# selective-suspend wake in TWO ways, both seen in the field on the FOX:
#   1. sd.InputStream raises PaErrorCode -9999 "device ID out of range";
#   2. the device isn't in sd.query_devices() YET (mid-enumeration) so
#      resolve-by-name comes back empty → "микрофон не найден".
# The old 2×180 ms didn't cover the wake window (~1-2 s typical, up to ~20 s
# worst case). We now retry up to 5 times with a 0.2/0.4/0.8/1.2 s backoff
# (~2.6 s total) and RE-RESOLVE the device BY NAME before every attempt (the
# index may have shifted on re-enumeration). The user is holding the hotkey
# and still talking, so a couple seconds to land the stream is acceptable.
#
# We deliberately do NOT call sd._terminate()/sd._initialize() between attempts
# — issue #516 / #47: refreshing PortAudio's cache mid-process is documented to
# lose devices and can crash the interpreter. Production log confirms it: FOX
# disappeared after the refresh and never came back without an app restart.
_OPEN_MAX_ATTEMPTS = 5
# Backoff before attempts 2..5 (index = attempt-1). Sums to ~2.6 s.
_OPEN_RETRY_BACKOFF_SEC = (0.2, 0.4, 0.8, 1.2)

# Target rate for the realtime WS chunk callback. Scribe v2 Realtime is happy
# with 16/24/44.1/48 kHz, but 16 kHz minimizes the JSON+base64 payload on the
# wire (44.1 → 16 is 2.75× less bytes) and matches the model's native rate.
# Resampling is in-place via numpy linear interpolation — sub-millisecond on
# a 1024-frame block, so it fits in the PortAudio callback budget.
_RT_TARGET_SR = 16000


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
        # Serializes the whole open (incl. A4 retries) against stop, so a stop
        # can never miss a stream that is still opening and leave it running.
        self._stream_lock = threading.Lock()
        self._start_time: float = 0
        # The mic is identified BY NAME (Windows device indices are unstable).
        # _device_id is only a legacy hint used when no name is configured.
        self._device_name: Optional[str] = None
        self._device_hostapi: Optional[str] = None
        self._device_id: Optional[int] = None  # None = use system default

        # Real-time audio level for visualization
        self._current_level: float = 0.0
        self._level_lock = threading.Lock()

        # Optional realtime-STT chunk hook. When set, the PortAudio callback
        # also pushes a resampled PCM16/16-kHz mono blob to this function on
        # every block. Raw float32 frames keep accumulating in _frames so a
        # fallback (WS dies → batch from the in-memory WAV) is always possible.
        self._chunk_callback: Optional[Callable[[bytes], None]] = None

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """Callback for audio stream"""
        if status:
            # PortAudio buffer events (input_overflow / input_underflow) used
            # to go to stdout which was a black hole — promoted to the logger
            # so transcription truncation can be correlated with buffer loss.
            logging.warning(f"PortAudio status: {status}")
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

            # Realtime-STT branch: push a resampled PCM16/16 kHz blob to the
            # chunk hook. Best-effort — any exception in the hook is logged
            # and swallowed so the main recording path is never affected.
            cb = self._chunk_callback
            if cb is not None:
                try:
                    cb(self._to_rt_pcm16(indata))
                except Exception as e:
                    logging.warning(f"chunk_callback failed: {e}")

    def _to_rt_pcm16(self, indata: np.ndarray) -> bytes:
        """Convert a sounddevice float32 block to PCM16 mono @16 kHz bytes.

        Uses linear interpolation for the resample. On a 1024-frame block at
        44.1 kHz this completes in well under 1 ms, comfortably within the
        PortAudio callback budget.
        """
        mono = indata.reshape(-1) if indata.ndim > 1 else indata
        if self.sample_rate == _RT_TARGET_SR:
            resampled = mono.astype(np.float32, copy=False)
        else:
            new_len = max(1, int(round(len(mono) * _RT_TARGET_SR / self.sample_rate)))
            x_old = np.linspace(0.0, 1.0, len(mono), endpoint=False)
            x_new = np.linspace(0.0, 1.0, new_len, endpoint=False)
            resampled = np.interp(x_new, x_old, mono).astype(np.float32)
        pcm16 = (np.clip(resampled, -1.0, 1.0) * 32767.0).astype(np.int16)
        return pcm16.tobytes()

    def set_chunk_callback(self, callback: Optional[Callable[[bytes], None]]):
        """Install / remove the realtime-STT chunk hook.

        Pass ``None`` to disable. Safe to call between recordings; do NOT
        call mid-recording — callback identity is captured per block, so a
        swap mid-stream would deliver part of one clip to the new sink.
        """
        self._chunk_callback = callback

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

    def start_recording(self, on_retry: Optional[Callable[[int, int], None]] = None):
        """Start recording audio from the configured mic (resolved BY NAME).

        Raises RuntimeError if a specific microphone was configured but is not
        present after all retry attempts — we never silently fall back to the
        system default.

        Open path: resolve the device BY NAME, query its native samplerate and
        open at that rate. On a transient failure (-9999 open error OR the
        device not in the list yet, both common after a USB-suspend wake) we
        wait with backoff, re-resolve, and try again up to _OPEN_MAX_ATTEMPTS.
        ``on_retry(attempt, total)`` (if given) is called before each wait so
        the UI can show "Подключаю микрофон…". We do NOT rebuild PortAudio's
        device cache here — see _OPEN_MAX_ATTEMPTS note.
        """
        with self._stream_lock:
            self._close_orphan_stream()
            self._open_stream(on_retry)

    def _close_orphan_stream(self):
        """Close a stream left open by a start without a matching stop (D8).

        Overwriting self._stream used to orphan a live PortAudio stream: it
        kept feeding _frames (clips captured twice), and since sounddevice has
        no __del__, GC later freed its cffi callback while PortAudio still
        called it → 0xc0000005 crash with no traceback.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            logging.warning(
                "Previous audio stream was still open at start — closing it "
                "(orphan guard)"
            )
            self._close_quietly(stream)

    @staticmethod
    def _close_quietly(stream):
        # Pa_CloseStream aborts an active stream first, so the callback stops.
        try:
            stream.close()
        except Exception as e:
            logging.warning(f"Closing audio stream failed: {e}")

    def _open_stream(self, on_retry: Optional[Callable[[int, int], None]]):
        last_err: Optional[Exception] = None
        for attempt in range(_OPEN_MAX_ATTEMPTS):
            if attempt > 0:
                # Tell the UI we're reconnecting, then back off so the device /
                # driver has time to finish a USB wakeup / MME WaveIn re-arm /
                # re-enumeration. No cache refresh.
                if on_retry is not None:
                    try:
                        on_retry(attempt + 1, _OPEN_MAX_ATTEMPTS)
                    except Exception:
                        pass
                backoff = _OPEN_RETRY_BACKOFF_SEC[
                    min(attempt - 1, len(_OPEN_RETRY_BACKOFF_SEC) - 1)
                ]
                time.sleep(backoff)

            # Re-resolve BY NAME every attempt — the index can shift on
            # re-enumeration, and a device absent now may reappear mid-window.
            try:
                device_index = self._resolve_device_for_capture()
            except RuntimeError as e:
                # Configured mic not in the CURRENT list. On a USB-suspend wake
                # it often reappears within a second or two, so treat this as a
                # retryable condition within the attempt budget rather than
                # failing the press outright.
                last_err = e
                logging.warning(
                    f"Microphone not resolved (attempt {attempt + 1}/"
                    f"{_OPEN_MAX_ATTEMPTS}): {e}"
                )
                continue

            capture_sr = self._query_native_samplerate(device_index)

            with self._lock:
                self._frames = []
                self.is_recording = True
                self._start_time = time.time()
                self.sample_rate = capture_sr
            with self._level_lock:
                self._current_level = 0.0

            stream = None
            try:
                stream = sd.InputStream(
                    samplerate=capture_sr,
                    channels=self.channels,
                    dtype=np.float32,
                    callback=self._audio_callback,
                    blocksize=1024,
                    device=device_index
                )
                stream.start()
                self._stream = stream
                if attempt == 0:
                    logging.info(f"Recording at native {capture_sr} Hz")
                else:
                    logging.info(
                        f"Recording at native {capture_sr} Hz "
                        f"(recovered on attempt {attempt + 1})"
                    )
                return  # success
            except Exception as e:
                # Roll back so a failed open can't wedge the recording state.
                with self._lock:
                    self.is_recording = False
                if stream is not None:  # opened but start() failed — don't leak it
                    self._close_quietly(stream)
                last_err = e
                is_9999 = "-9999" in str(e) or "out of range" in str(e).lower()
                logging.warning(
                    f"InputStream open failed (attempt {attempt + 1}/"
                    f"{_OPEN_MAX_ATTEMPTS}"
                    f"{'; -9999 device out of range' if is_9999 else ''}): {e}"
                )

        # All attempts exhausted — surface the last error to the caller.
        raise last_err if last_err is not None else RuntimeError(
            "Не удалось открыть микрофон"
        )

    def stop_recording(self) -> tuple[bytes, int]:
        """Stop recording and return audio data as WAV bytes and duration in ms"""
        # Waits for an in-flight open to finish, then closes THAT stream.
        with self._stream_lock:
            self.is_recording = False
            duration_ms = int((time.time() - self._start_time) * 1000)

            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()

        with self._lock:
            if not self._frames:
                return b"", 0

            chunks = len(self._frames)
            audio_data = np.concatenate(self._frames, axis=0)
            self._frames = []

        # Compare wall-clock duration with actual captured audio length —
        # a large gap means PortAudio dropped frames (input_overflow) or
        # the stream stopped mid-recording. Either way, the audio sent to
        # Scribe is shorter than the user expects → "обрезает голос".
        actual_ms = int(round(len(audio_data) / float(self.sample_rate) * 1000))
        if duration_ms > 500 and actual_ms < duration_ms * 0.7:
            logging.warning(
                f"Audio captured ({actual_ms}ms, {chunks} chunks) is much "
                f"shorter than wall-clock ({duration_ms}ms) — likely "
                f"PortAudio frame loss. Sample rate={self.sample_rate}"
            )
        else:
            logging.info(
                f"Audio: wall {duration_ms}ms / captured {actual_ms}ms "
                f"({chunks} chunks @ {self.sample_rate}Hz)"
            )

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
