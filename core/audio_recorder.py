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

# How many times to try opening the input stream within a SINGLE key press.
# Attempt 1 uses PortAudio's cached device list (fast, zero added latency when
# nothing is wrong); later attempts rebuild that list first (cure for a stale
# cache after the audio device set changes mid-run — PaErrorCode -9999).
_OPEN_MAX_ATTEMPTS = 4

# After rebuilding the device list, give PortAudio/Windows a moment to finish
# re-enumerating before querying it again. Re-initialising and immediately
# querying returns a half-built list (the mic momentarily looks "not found"),
# and hammering terminate/initialize back-to-back leaves the host API in a bad
# state — that is exactly why recovery used to need several key presses. The
# settle grows per attempt (0.25s, 0.50s, 0.75s) so recovery happens within ONE
# press; only paid on the slow path, never on a healthy mic.
_REFRESH_SETTLE_SEC = 0.25

# Background watcher poll interval. Also acts as the settle between its refreshes:
# rebuilding the list at most every couple of seconds gives a slow USB mic time
# to finish re-enumerating, instead of the harmful back-to-back hammering.
_WATCHER_POLL_SEC = 2.0


class RecordingAborted(Exception):
    """The start was cancelled (hotkey released) before recording could begin.

    Not an error: the user let go while we were still retrying a flaky mic, so
    there is simply nothing to record. Callers should reset quietly — no error
    cue or notification.
    """


class AudioRecorder:
    def __init__(self):
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

        # Serialises every PortAudio re-init (terminate/initialize) so the
        # background watcher and a key press can never rebuild the device list
        # at the same time. NEVER hold this while a stream is open.
        self._pa_lock = threading.Lock()

        # Background device watcher: keeps PortAudio's view of the configured mic
        # fresh OUT of the key-press path, so a slow-to-reappear USB mic is ready
        # by the time the user actually presses the hotkey.
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_stop = threading.Event()
        self._mic_present: bool = True  # last known state, to log only on change

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

    def _refresh_portaudio(self):
        """Rebuild PortAudio's device list to match the CURRENT system state.

        PortAudio enumerates devices once at process init and caches that list.
        When the set of audio devices changes mid-run (a Bluetooth headset
        connects, the USB mic power-cycles, Windows re-enumerates), the cached
        index for our mic goes stale and opening it fails with
        PaErrorCode -9999 'device ID out of range'. Tearing PortAudio down and
        re-initialising forces a fresh scan so the next resolve+open sees reality.
        Must only be called when no stream is open.
        """
        try:
            sd._terminate()
            sd._initialize()
            logging.info("Refreshed PortAudio device list (was stale)")
        except Exception as e:
            logging.error(f"PortAudio refresh failed: {e}", exc_info=True)

    def start_recording(self, should_abort: Optional[Callable[[], bool]] = None):
        """Start recording audio from the configured mic (resolved BY NAME).

        Raises RuntimeError if a specific microphone was configured but is not
        currently present — we never silently fall back to the system default.

        The open is retried, refreshing PortAudio's (possibly stale) device cache
        and letting it settle between tries: that is the in-process cure for the
        intermittent 'device ID out of range' failures that otherwise need an app
        restart. Because the settle is paced, recovery now completes within a
        single key press instead of taking several.

        should_abort: optional predicate polled between tries. When the hotkey is
        released mid-retry it returns True; we then stop and raise
        RecordingAborted instead of starting a recording nobody asked for.
        """
        def aborted() -> bool:
            return should_abort is not None and should_abort()

        last_err: Optional[Exception] = None
        # Hold the PortAudio lock for the whole open sequence so the background
        # watcher can't rebuild the device list while we resolve/open (released
        # the moment recording starts — never held during the recording itself).
        with self._pa_lock:
            # Attempt 0 uses the cached device list (fast path, no delay). Later
            # attempts rebuild the list and let it settle — slow path only on failure.
            for attempt in range(_OPEN_MAX_ATTEMPTS):
                if aborted():
                    raise RecordingAborted()

                if attempt > 0:
                    self._refresh_portaudio()
                    time.sleep(_REFRESH_SETTLE_SEC * attempt)  # let it re-enumerate
                    if aborted():
                        raise RecordingAborted()

                try:
                    device_index = self._resolve_device_for_capture()
                except RuntimeError as e:
                    # Mic not found in the current list — a refresh may reveal it.
                    last_err = e
                    logging.warning(f"Mic resolve failed (attempt {attempt + 1}): {e}")
                    continue

                with self._lock:
                    self._frames = []
                    self.is_recording = True
                    self._start_time = time.time()
                with self._level_lock:
                    self._current_level = 0.0

                try:
                    self._stream = sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=self.channels,
                        dtype=np.float32,
                        callback=self._audio_callback,
                        blocksize=1024,
                        device=device_index
                    )
                    self._stream.start()
                except Exception as e:
                    # Roll back so a failed open can't wedge the recording state.
                    with self._lock:
                        self.is_recording = False
                    self._stream = None
                    last_err = e
                    logging.warning(
                        f"InputStream open failed (attempt {attempt + 1}): {e}"
                    )
                    continue

                # Stream is live. If the key was released while we were (re)trying,
                # tear it down rather than leave a recording that nobody will stop.
                if aborted():
                    try:
                        self._stream.stop()
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                    with self._lock:
                        self.is_recording = False
                    raise RecordingAborted()

                if attempt > 0:
                    logging.info(f"Recording recovered on attempt {attempt + 1}")
                return  # success

            # All attempts exhausted — surface the last error to the caller.
            assert last_err is not None
            raise last_err

    def start_device_watcher(self):
        """Start the background thread that keeps the configured mic resolvable.

        A slow USB mic (or one that drops when another audio device connects)
        can be absent from PortAudio's list for several seconds — far longer than
        a key press can afford to block. Instead of making the user mash the
        hotkey, this rebuilds the device list every couple of seconds IN THE
        BACKGROUND until the mic resolves, then idles. So by the time the hotkey
        is actually pressed, the list is already fresh and recording starts at
        once. Safe no-op if already running or if no specific mic is configured.
        """
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._device_watcher_loop, daemon=True, name="mic-watcher"
        )
        self._watcher_thread.start()
        logging.info("Mic device watcher started")

    def stop_device_watcher(self):
        """Stop the background device watcher (called on shutdown)."""
        self._watcher_stop.set()

    def _device_watcher_loop(self):
        while not self._watcher_stop.wait(_WATCHER_POLL_SEC):
            # Only meaningful when a specific mic is configured (resolve-by-name).
            if not self._device_name:
                continue
            # Never rebuild PortAudio while capturing — re-init with an open
            # stream is unsafe. The lock + these guards keep us clear of a press.
            if self.is_recording or self._stream is not None:
                continue
            try:
                if not self._pa_lock.acquire(blocking=False):
                    continue  # a key press owns PortAudio right now; try next tick
                try:
                    if self.is_recording or self._stream is not None:
                        continue
                    idx, detail = self.resolve_device_index()
                    if idx is None and self._device_name:
                        # Mic missing: rebuild the list so the next press sees it.
                        self._refresh_portaudio()
                        idx, detail = self.resolve_device_index()
                finally:
                    self._pa_lock.release()
            except Exception as e:
                logging.error(f"Device watcher error: {e}", exc_info=True)
                continue

            present = idx is not None
            if present != self._mic_present:
                self._mic_present = present
                if present:
                    logging.info(f"Device watcher: mic back online -> {detail}")
                else:
                    logging.warning(
                        f"Device watcher: configured mic «{self._device_name}» "
                        f"not present; refreshing until it returns"
                    )

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
