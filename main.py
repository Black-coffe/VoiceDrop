"""
VoiceDrop - Voice to Text Application
Main entry point
"""
import asyncio
import concurrent.futures
import os
import sys
import threading
import time
import winsound
import logging
from logging.handlers import RotatingFileHandler
import atexit
import signal
from typing import Optional
from datetime import datetime

# Setup file logging FIRST to capture all events
def get_app_dir():
    """Get application directory that works for both development and PyInstaller"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(__file__)

APP_DIR = get_app_dir()
LOG_FILE = os.path.join(APP_DIR, 'voicedrop.log')
CRASH_LOG_FILE = os.path.join(APP_DIR, 'voicedrop_crash.log')
# RotatingFileHandler so voicedrop.log can't grow unbounded (it had reached
# 16 MB before this). 5 files × 2 MB = ~10 MB ceiling of recent history.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8'
        ),
        logging.StreamHandler(sys.stdout)
    ]
)


# --- Global crash handlers ---------------------------------------------------
# Real crashes used to vanish without a trace: all tracebacks in voicedrop.log
# came from the caught network errors in _process_audio, while uncaught failures
# in daemon threads (hotkey callbacks, pystray) or native code died silently.
# These hooks make every uncaught exception land in voicedrop_crash.log.
def _write_crash(kind: str, exc_type, exc_value, exc_tb, thread_name: str = ""):
    import traceback
    tb_text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    where = f" in thread '{thread_name}'" if thread_name else ""
    header = f"{kind}{where}: {getattr(exc_type, '__name__', exc_type)}: {exc_value}"
    logging.critical("%s\n%s", header, tb_text)
    try:
        with open(CRASH_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"\n{'=' * 70}\n{datetime.now().isoformat()}  {header}\n{tb_text}")
    except Exception:
        pass  # never let crash logging itself crash


def _sys_excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _write_crash("UNCAUGHT EXCEPTION (main thread)", exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    if issubclass(args.exc_type, SystemExit):
        return
    thread_name = getattr(args.thread, 'name', '') if args.thread else ''
    _write_crash("UNCAUGHT EXCEPTION (thread)",
                 args.exc_type, args.exc_value, args.exc_traceback, thread_name)


sys.excepthook = _sys_excepthook
threading.excepthook = _thread_excepthook
logging.info("Global crash handlers installed (crash log: %s)", CRASH_LOG_FILE)
# ----------------------------------------------------------------------------

# Fix Windows console encoding for Russian text
if sys.platform == 'win32':
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import ctypes

    # Single instance check using Windows mutex
    # Use full path to make mutex unique to this installation
    MUTEX_NAME = f"Global\\VoiceDrop_{os.path.abspath(APP_DIR).replace(':', '').replace('\\', '_')}"

    # Create mutex and store as GLOBAL variable to prevent garbage collection
    _APP_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        logging.warning("Application already running, exiting...")
        sys.exit(0)

    logging.info(f"Mutex created: {MUTEX_NAME}")

    # Hold mutex reference to prevent GC
    def _release_mutex():
        if _APP_MUTEX:
            ctypes.windll.kernel32.ReleaseMutex(_APP_MUTEX)
            ctypes.windll.kernel32.CloseHandle(_APP_MUTEX)
            logging.info("Mutex released")

    atexit.register(_release_mutex)

    # Set AppUserModelID for proper taskbar icon on Windows
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('VoiceDrop.App')

import customtkinter as ctk
from apscheduler.schedulers.background import BackgroundScheduler

from config import ELEVENLABS_API_KEY, HISTORY_RETENTION_HOURS, APP_VERSION
from core import autostart
from core.audio_muter import AudioMuter
from core.audio_recorder import AudioRecorder
from core.db_manager import DatabaseManager
from core.elevenlabs_api import ElevenLabsClient, TranscriptionError
from core.elevenlabs_ws import (
    END_OF_STREAM,
    RealtimeError,
    RealtimeTranscriber,
)
from core.hotkey_manager import HotkeyManager
from core.keyterms import KeyTerms
from core.pending_queue import PendingQueue
from core.profiles import ProfileManager
from core.text_replacer import TextReplacer
from core.text_polisher import TextPolisher
from core.usage_tracker import UsageTracker
from core.voice_commands import VoiceCommands
from core.text_inserter import TextInserter
from gui.history_window import HistoryWindow
from gui.recording_overlay import RecordingOverlay
from gui.settings_window import SettingsWindow, load_settings, save_settings
from gui.tray_icon import TrayIcon


# Don't mute other apps for ultra-short presses (they become "too short" anyway).
# This kills the mute/unmute churn that flaps other apps' audio on quick taps.
_MUTE_GRACE_SEC = 0.18
# Suppress stacked start-beeps when the hotkey is tapped rapidly.
_START_BEEP_MIN_GAP_SEC = 0.25

# Display name per language code (None = auto-detect) for the tray quick-toggle.
_LANGUAGE_NAMES = {None: "Автоопределение", "ru": "Русский", "uk": "Українська", "en": "English"}


class VoiceDropApp:
    def __init__(self):
        logging.info("Initializing VoiceDropApp...")
        self.audio_recorder = AudioRecorder()
        self.audio_muter = AudioMuter()
        self.elevenlabs_client = ElevenLabsClient()
        self._apply_network_settings()
        # Warm up the ElevenLabs HTTPS connection in the background so the first
        # transcribe() of the session doesn't pay TLS handshake + DNS (~100–300 ms).
        # Daemon thread + best-effort inside warm_up() — never blocks startup.
        threading.Thread(target=self.elevenlabs_client.warm_up, daemon=True).start()
        self.text_inserter = TextInserter()
        self.db = DatabaseManager()
        self.db.set_retention(load_settings().get('history_retention_hours', HISTORY_RETENTION_HOURS))
        self.pending_queue = PendingQueue()
        self.text_replacer = TextReplacer()
        self.text_polisher = TextPolisher()
        # Surface repeated polish failures instead of silently pasting raw text
        # (A5). Reason is shown once via tray notification + marked in the menu.
        self._polish_failure_reason: Optional[str] = None
        self.text_polisher.on_repeated_failure = self._on_polish_repeated_failure
        self.text_polisher.on_recovered = self._on_polish_recovered
        self.voice_commands = VoiceCommands()
        self.key_terms = KeyTerms()  # B2: hot-reloaded keyterms.json (opt-in)
        self.profiles = ProfileManager()
        self.usage = UsageTracker()
        self.realtime_transcriber = RealtimeTranscriber(ELEVENLABS_API_KEY)
        # Dedicated asyncio loop in a daemon thread for realtime WS sessions.
        # Created lazily on first realtime-mode press so batch users pay nothing.
        self._rt_loop: Optional[asyncio.AbstractEventLoop] = None
        self._rt_thread: Optional[threading.Thread] = None
        self._rt_loop_ready = threading.Event()
        # Per-press state for an in-flight realtime session.
        self._rt_chunk_queue: Optional[asyncio.Queue] = None
        self._rt_active_future: Optional[concurrent.futures.Future] = None
        self._rt_language: Optional[str] = None

        self.tray_icon: Optional[TrayIcon] = None
        self.history_window: Optional[HistoryWindow] = None
        self.settings_window: Optional[SettingsWindow] = None
        self.hotkey_manager: Optional[HotkeyManager] = None
        self.recording_overlay: Optional[RecordingOverlay] = None

        self._is_recording = False
        # True while _process_audio is doing the post-release work (transcribe
        # + polish + insert). A new press during this window would race the
        # shared AudioRecorder state (frames buffer, stream handle) and corrupt
        # the next clip — see the truncation bug user hit on rapid presses.
        self._is_processing = False
        self._lock = threading.Lock()
        self._is_shutting_down = False
        # Monotonic id per record session — lets a late/slow mute detect that its
        # recording already ended and undo itself, preventing "stuck muted" audio.
        self._record_session_id = 0
        self._last_start_beep = 0.0
        # Last recording's audio, kept so it can be re-transcribed in another
        # language (one-click fix for RU/UK/EN auto-detect bleed).
        self._last_audio_data: Optional[bytes] = None
        self._last_duration_ms = 0
        self._last_text = ""  # last produced text (for tray "Скопировать последнее")

        # Safety unmute on startup (D2): if a previous instance was killed mid-
        # recording (kill / BSOD), the apps we muted stay muted forever. Clear
        # them now, plus a delayed second sweep for sessions that only become
        # enumerable a moment after launch (the muted app resumes audio).
        self._safety_unmute_on_startup()

        # Load saved settings and get hotkey VK codes
        self._saved_hotkey_vks = self._apply_saved_settings()

        # "Code mode" modifier: hold it WITH the hotkey to dictate this clip raw
        # (no polish). Default Right Shift (161); ignored if it's part of the hotkey.
        _cm = load_settings().get('code_modifier', 161)
        _primary = self._saved_hotkey_vks or set()
        self._code_modifier_vks = {_cm} if (_cm and _cm not in _primary) else set()

        # Background scheduler for cleanup
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(self.db.cleanup_old_recordings, 'interval', hours=1)
        # Auto-resend recordings that failed transcription (offline / outage).
        self.scheduler.add_job(
            self._process_pending_queue, 'interval', seconds=45,
            max_instances=1, coalesce=True
        )

        # Hidden root window for customtkinter
        self._root: Optional[ctk.CTk] = None

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logging.info("VoiceDropApp initialized")

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        logging.warning(f"Received signal {signum}, initiating shutdown...")
        self._on_quit()

    # --- Realtime STT plumbing ------------------------------------------------
    def _get_stt_mode(self) -> str:
        """Return 'batch' (default) or 'realtime' — read fresh each press so
        the user can toggle in Settings without restarting."""
        mode = (load_settings().get("stt_mode") or "batch").strip().lower()
        return mode if mode in ("batch", "realtime") else "batch"

    def _ensure_rt_loop(self):
        """Lazily start a daemon asyncio loop for realtime WS sessions."""
        if self._rt_loop is not None and self._rt_thread and self._rt_thread.is_alive():
            return

        def _run():
            loop = asyncio.new_event_loop()
            self._rt_loop = loop
            asyncio.set_event_loop(loop)
            self._rt_loop_ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    pending = asyncio.all_tasks(loop=loop)
                    for t in pending:
                        t.cancel()
                except Exception:
                    pass
                loop.close()

        self._rt_loop_ready.clear()
        self._rt_thread = threading.Thread(target=_run, daemon=True, name="rt-loop")
        self._rt_thread.start()
        # Wait briefly so callers can rely on self._rt_loop being usable.
        self._rt_loop_ready.wait(timeout=2.0)

    def _stop_rt_loop(self):
        """Best-effort shutdown of the realtime asyncio loop."""
        loop = self._rt_loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._rt_thread and self._rt_thread.is_alive():
            self._rt_thread.join(timeout=2.0)
        self._rt_loop = None
        self._rt_thread = None

    async def _run_realtime_session(
        self,
        chunk_queue: asyncio.Queue,
        language: Optional[str],
    ) -> str:
        """Bridge: forward partial_transcript to the overlay (via Tk-safe
        ``after(0, …)``) and run the transcriber's WS session."""
        def on_partial(text: str):
            if self.recording_overlay and self._root:
                try:
                    self._root.after(
                        0,
                        lambda t=text: self.recording_overlay.show_polish_partial(t),
                    )
                except Exception:
                    pass

        # B3: opt-in server-side VAD commit + its silence threshold (advanced).
        s = load_settings()
        commit_strategy = (s.get('rt_commit_strategy') or 'manual').strip().lower()
        vad_thresh = s.get('rt_vad_silence_threshold_secs')
        try:
            vad_thresh = float(vad_thresh) if vad_thresh is not None else None
        except (TypeError, ValueError):
            vad_thresh = None

        return await self.realtime_transcriber.transcribe_stream(
            chunk_queue=chunk_queue,
            sample_rate=16000,
            on_partial=on_partial,
            language=language,
            no_verbatim=self._get_no_verbatim(),       # B1
            keyterms=self._get_keyterms(realtime=True),  # B2
            commit_strategy=commit_strategy,            # B3
            vad_silence_threshold_secs=vad_thresh,       # B3
        )

    def _rt_enqueue_chunk(self, chunk: bytes):
        """Audio-callback-thread → asyncio queue. Survives loop being gone."""
        loop = self._rt_loop
        queue = self._rt_chunk_queue
        if loop is None or queue is None:
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except RuntimeError:
            # Loop closed mid-recording — give up silently; release handler
            # will surface a RealtimeError on future.result() and fall back.
            pass

    def _on_hotkey_press(self):
        """Called when hotkey is pressed - start recording.

        Capture starts FIRST so muting (slow pycaw/COM) can never delay or eat
        the recording. Muting runs in a background, race-guarded worker.
        """
        try:
            with self._lock:
                if self._is_recording:
                    return
                if self._is_processing:
                    # Previous clip is still being transcribed / inserted.
                    # Starting now would share the AudioRecorder's _frames /
                    # _stream state with the in-flight pipeline and corrupt
                    # the new clip — observed as Scribe returning a few words
                    # then "..." or "--".
                    logging.info("Hotkey press ignored: previous clip still processing")
                    # Soft error cue so the user knows the press registered
                    # but was rejected. They retry in ~1 s after Scribe replies.
                    winsound.Beep(300, 80)
                    return
                self._is_recording = True
                self._record_session_id += 1
                session_id = self._record_session_id

            logging.info("Recording started...")

            # Audio cue first (kept out of the captured audio), then capture.
            # Rate-limited so rapid taps don't stack beeps into noise.
            now = time.time()
            if now - self._last_start_beep > _START_BEEP_MIN_GAP_SEC:
                self._last_start_beep = now
                winsound.Beep(600, 50)

            # 1) START CAPTURE IMMEDIATELY — no blocking work before this.
            # on_retry surfaces "Подключаю микрофон…" in the overlay while the
            # recorder re-resolves + backs off through a USB-suspend wake (A4).
            try:
                self.audio_recorder.start_recording(on_retry=self._on_mic_retry)
            except Exception as e:
                logging.error(f"Failed to start recording: {e}", exc_info=True)
                with self._lock:
                    self._is_recording = False
                # Make sure we never leave the system muted or the UI stuck.
                try:
                    self.audio_muter.unmute_all()
                except Exception:
                    pass
                if self.tray_icon:
                    self.tray_icon.set_recording(False)
                    self.tray_icon.show_notification("VoiceDrop — микрофон", str(e))
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                winsound.Beep(300, 200)  # error cue
                return

            # 2) UI feedback.
            if self.tray_icon:
                self.tray_icon.set_recording(True)
            if self._root and self.recording_overlay:
                self._root.after(0, lambda: self.recording_overlay.show(self._root))

            # 3) Mute other apps in the background (best-effort, race-guarded).
            threading.Thread(
                target=self._mute_worker, args=(session_id,), daemon=True
            ).start()

            # 4) Realtime STT branch — start a WS session in the rt loop and
            # have the audio callback push PCM16/16k chunks into its queue.
            # Raw float32 frames keep accumulating in audio_recorder so a
            # WS failure can still fall back to batch from the in-memory WAV.
            if self._get_stt_mode() == "realtime":
                try:
                    self._ensure_rt_loop()
                    settings = load_settings()
                    self._rt_language = settings.get("language_code", None)
                    # asyncio.Queue() can be constructed from any thread in
                    # 3.10+; it captures the running loop on first .get/.put,
                    # which will happen inside the rt loop.
                    self._rt_chunk_queue = asyncio.Queue()
                    self._rt_active_future = asyncio.run_coroutine_threadsafe(
                        self._run_realtime_session(
                            self._rt_chunk_queue, self._rt_language
                        ),
                        self._rt_loop,
                    )
                    self.audio_recorder.set_chunk_callback(self._rt_enqueue_chunk)
                    logging.info("Realtime STT session started")
                except Exception as e:
                    # Don't bring down the press — log and silently fall back
                    # to batch for this clip. The recorder keeps recording
                    # either way.
                    logging.warning(
                        f"Realtime STT setup failed, this clip falls back to batch: {e}"
                    )
                    self._rt_active_future = None
                    self._rt_chunk_queue = None
                    self.audio_recorder.set_chunk_callback(None)
        except Exception as e:
            logging.error(f"Unexpected error in _on_hotkey_press: {e}", exc_info=True)
            with self._lock:
                self._is_recording = False

    def _safety_unmute_on_startup(self):
        """D2: clear any mutes left behind by a crashed previous instance.

        Runs an immediate force-unmute (non-whitelisted sessions that are still
        muted) plus one delayed re-sweep ~3 s later — a muted app whose audio
        session wasn't active at process launch (paused at crash time) only
        becomes enumerable once it resumes, and would otherwise stay muted.
        """
        logging.info("Safety unmute on startup (D2)...")
        try:
            self.audio_muter.force_unmute_all()
        except Exception as e:
            logging.error(f"Startup force_unmute_all failed: {e}", exc_info=True)

        def _delayed_sweep():
            try:
                self.audio_muter.force_unmute_all()
                logging.debug("Startup delayed unmute sweep done (D2)")
            except Exception as e:
                logging.debug(f"Startup delayed unmute sweep failed: {e}")

        t = threading.Timer(3.0, _delayed_sweep)
        t.daemon = True
        t.start()

    def _on_mic_retry(self, attempt: int, total: int):
        """Called by AudioRecorder before each mic-open retry (A4). Logs the
        attempt and shows a 'reconnecting' state in the overlay."""
        logging.info(f"Reconnecting microphone… (attempt {attempt}/{total})")
        if self._root and self.recording_overlay:
            self._root.after(
                0, lambda: self.recording_overlay.show_connecting(self._root)
            )

    def _mute_worker(self, session_id: int):
        """Mute other audio sessions for a given record session.

        If the recording already ended (or a newer one started) by the time the
        mute completes, undo it — this is what prevents audio getting stuck muted
        when pycaw's COM call is slow and lands after release.
        """
        # Grace period: a quick tap ends within this window and never mutes,
        # so other apps' audio doesn't flap on accidental / too-short presses.
        time.sleep(_MUTE_GRACE_SEC)
        with self._lock:
            if not (self._is_recording and session_id == self._record_session_id):
                return  # released before muting even began (or a newer session started)
        try:
            self.audio_muter.mute_all()
        except Exception as e:
            logging.error(f"mute_all failed: {e}", exc_info=True)
            return
        with self._lock:
            still_active = self._is_recording and session_id == self._record_session_id
        if not still_active:
            try:
                self.audio_muter.unmute_all()
                logging.info("Mute landed after release; auto-unmuted (race guard)")
            except Exception as e:
                logging.error(f"race-guard unmute failed: {e}", exc_info=True)

    def _on_hotkey_release(self):
        """Called when hotkey is released - stop recording and process."""
        try:
            with self._lock:
                if not self._is_recording:
                    return
                self._is_recording = False

            logging.info("Recording stopped, processing...")

            # Unmute always wins. If a late mute_worker lands after this, its own
            # race guard will detect the ended session and unmute again.
            try:
                self.audio_muter.unmute_all()
            except Exception as e:
                logging.error(f"unmute_all failed: {e}", exc_info=True)

            if self.tray_icon:
                self.tray_icon.set_recording(False)

            # Show processing state on overlay
            if self.recording_overlay and self._root:
                self._root.after(0, self.recording_overlay.show_processing)

            # Stop recording
            try:
                audio_data, duration_ms = self.audio_recorder.stop_recording()
            except Exception as e:
                logging.error(f"stop_recording failed: {e}", exc_info=True)
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return

            min_ms = load_settings().get('min_duration_ms', 200)
            if not audio_data or duration_ms < min_ms:  # Too short
                logging.info("Recording too short, ignoring")
                # If a realtime session was in flight, abort it before we
                # leave — don't leak the WS task or the chunk callback.
                if self._rt_active_future is not None:
                    self.audio_recorder.set_chunk_callback(None)
                    self._rt_active_future.cancel()
                    self._rt_active_future = None
                    self._rt_chunk_queue = None
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return

            # Code-mode modifier (e.g. Right Shift) held with the hotkey -> raw
            forced_mode = "code" if (self.hotkey_manager and
                                     self.hotkey_manager.modifier_was_held()) else None

            # Dispatch to the right processor — realtime session was set up
            # in _on_hotkey_press if stt_mode == 'realtime' AND the setup
            # succeeded. We pin to the future/queue snapshot here so a fresh
            # press during processing can't see stale state.
            if self._rt_active_future is not None:
                rt_future = self._rt_active_future
                rt_queue = self._rt_chunk_queue
                rt_lang = self._rt_language
                # Detach from the instance attrs immediately so a quick second
                # press is welcome to set up its own session.
                self._rt_active_future = None
                self._rt_chunk_queue = None
                self.audio_recorder.set_chunk_callback(None)
                # Signal end-of-stream so the WS sends commit=true and waits
                # for committed_transcript.
                try:
                    if self._rt_loop and rt_queue is not None:
                        self._rt_loop.call_soon_threadsafe(
                            rt_queue.put_nowait, END_OF_STREAM
                        )
                except Exception as e:
                    logging.debug(f"Could not signal END_OF_STREAM: {e}")

                threading.Thread(
                    target=self._process_audio_realtime,
                    args=(audio_data, duration_ms, forced_mode,
                          rt_future, rt_lang),
                    daemon=True,
                ).start()
            else:
                # Batch path (default, unchanged).
                threading.Thread(
                    target=self._process_audio,
                    args=(audio_data, duration_ms, forced_mode),
                    daemon=True
                ).start()
        except Exception as e:
            logging.error(f"Unexpected error in _on_hotkey_release: {e}", exc_info=True)
            try:
                self.audio_muter.unmute_all()
            except Exception:
                pass

    def _process_audio(self, audio_data: bytes, duration_ms: int, forced_mode: Optional[str] = None):
        """Process audio: transcribe and insert text"""
        proc_t0 = time.monotonic()  # C5: release→inserted pipeline clock
        # Keep this clip so it can be re-transcribed in another language later.
        self._last_audio_data = audio_data
        self._last_duration_ms = duration_ms
        # Mark the "busy" window so _on_hotkey_press can reject mid-pipeline
        # presses cleanly. Released in finally regardless of success/failure.
        with self._lock:
            self._is_processing = True
        try:
            # Transcribe
            logging.info("Sending to ElevenLabs...")
            start_time = time.time()

            # Get language setting (None = auto-detect)
            settings = load_settings()
            language = settings.get('language_code', None)

            # Apply current no_verbatim / keyterms (B1/B2) before the request.
            self._refresh_batch_stt_options()
            text = self.elevenlabs_client.transcribe(audio_data, language=language)

            elapsed = time.time() - start_time
            logging.info(f"Transcribed in {elapsed:.2f}s: {text[:50]}...")

            if not text or not text.strip():
                logging.info("Empty transcription")
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return

            text = text.strip()
            self._finish_text(text, audio_data, duration_ms, forced_mode, language,
                              stt_mode="batch", stt_sec=elapsed, proc_t0=proc_t0)

        except TranscriptionError as e:
            # Transcription failed after retries. Queue it for auto-resend if the
            # cause is transient (offline / outage); skip queueing permanent
            # errors (400 audio_too_short, auth) that would never succeed.
            should_queue = e.offline or e.retryable
            queued = False
            if should_queue:
                queued = self.pending_queue.enqueue(audio_data, duration_ms, language) is not None

            if e.offline:
                logging.warning(f"Transcription failed (offline): {e}")
                title = "VoiceDrop — нет сети"
                msg = ("Нет интернета. Запись сохранена — дошлём автоматически."
                       if queued else "Не удалось связаться с ElevenLabs. Проверьте интернет.")
            elif should_queue:
                logging.error(f"Transcription failed (will retry later): {e}")
                title = "VoiceDrop — сервис недоступен"
                msg = ("Сервис недоступен. Запись сохранена — дошлём автоматически."
                       if queued else str(e))
            else:
                logging.error(f"Transcription failed: {e}")
                title = "VoiceDrop — ошибка"
                msg = str(e)

            winsound.Beep(300, 200)  # error beep
            if self.recording_overlay and self._root:
                self._root.after(0, lambda m=msg: self.recording_overlay.show_error(m))
                self._root.after(2500, self.recording_overlay.hide)
            if self.tray_icon:
                self.tray_icon.show_notification(title, msg)

        except Exception as e:
            logging.error(f"Error processing audio: {e}", exc_info=True)
            # Play error sound
            winsound.Beep(300, 200)  # Low-pitched error beep
            if self.recording_overlay and self._root:
                self._root.after(0, self.recording_overlay.hide)
            if self.tray_icon:
                self.tray_icon.show_notification("VoiceDrop — ошибка", str(e))
        finally:
            # Release the press-gate so the next hotkey press is accepted.
            with self._lock:
                self._is_processing = False

    def _finish_text(self, text: str, audio_data: bytes, duration_ms: int,
                     forced_mode: Optional[str], language: Optional[str],
                     stt_mode: str = "batch", stt_sec: float = 0.0,
                     proc_t0: Optional[float] = None):
        """Post-STT pipeline shared by batch and realtime paths.

        Takes a non-empty, stripped transcript and runs: polish → dictionary
        → voice commands → code-style → overlay → insert → history → usage.
        ``audio_data`` is only kept on the instance for "Переписать последнее";
        it's NOT re-uploaded here.
        """
        # Resolve dictation mode: hotkey modifier forces code, else the setting
        mode = forced_mode or self._effective_mode()
        if forced_mode == "code":
            logging.info("Code-mode modifier held -> raw (no polish) for this clip")

        # Optional LLM polish — text mode only (code mode stays verbatim).
        # Best-effort: returns original on error.
        polish_sec = 0.0
        if mode == "text" and self._get_polish_enabled():
            # Stream the polish into the overlay as it arrives — UX-only,
            # the actual paste below still uses the FINAL completed text.
            def _on_polish_partial(partial: str):
                if self.recording_overlay and self._root:
                    self._root.after(
                        0,
                        lambda p=partial: self.recording_overlay.show_polish_partial(p),
                    )
            _polish_t0 = time.monotonic()
            text = self.text_polisher.polish(
                text, language=language, on_partial=_on_polish_partial,
                min_words=self._get_polish_min_words(),  # C2: skip on short clips
            )
            polish_sec = time.monotonic() - _polish_t0
        # Custom dictionary: fix tech terms / names STT mangles (local, instant)
        text = self.text_replacer.apply(text)
        # Voice formatting commands: "новая строка", "код блок", ... -> symbols
        text = self.voice_commands.apply(text)
        # Code mode: verbatim style (drop trailing period, lowercase Latin start)
        if mode == "code":
            text = self.profiles.apply_code_style(text)

        # Show word count on overlay
        word_count = len(text.split())
        char_count = len(text)
        if self.recording_overlay and self._root:
            # Hold the final text in overlay for 800 ms so the streaming/typing
            # effect is readable on short clips (1–2 s polish was flashing by).
            # Then switch to the "N слов" summary and hide after another 1.5 s.
            # Doesn't delay paste — text is already inserted above.
            self._root.after(0, lambda t=text: self.recording_overlay.show_polish_partial(t))
            self._root.after(800, lambda: self.recording_overlay.show_result(word_count, char_count))
            self._root.after(800 + 1500, self.recording_overlay.hide)

        self._last_text = text  # remember for "Скопировать последнее"

        # Deliver text per the chosen insert mode
        insert_mode = self._get_insert_mode()
        _insert_t0 = time.monotonic()
        if insert_mode == "clipboard":
            self.text_inserter.copy_to_clipboard(text)
            inserted = False
        elif insert_mode == "enter":
            inserted = self.text_inserter.insert_text(text, press_enter=True)
        else:  # "window"
            inserted = self.text_inserter.insert_text(text)
        insert_sec = time.monotonic() - _insert_t0

        # Save to database (unless history saving is disabled for privacy)
        if self._get_save_history():
            self.db.save_recording(text, duration_ms, was_inserted=inserted)
        self.usage.record(duration_ms, len(text), mode=stt_mode)

        # Play success sound
        winsound.Beep(800, 100)  # Short high-pitched beep

        # C5: one line per clip for the whole release→inserted pipeline, so the
        # effect of every latency change (C1/C2/C3…) is visible at a glance.
        total_sec = (time.monotonic() - proc_t0) if proc_t0 is not None else 0.0
        logging.info(
            "Pipeline [%s]: capture %.1fs | stt %.2fs | polish %.2fs | "
            "insert %.2fs | total %.2fs (%d words)",
            stt_mode, duration_ms / 1000.0, stt_sec, polish_sec,
            insert_sec, total_sec, word_count,
        )

        logging.info(f"Done! Text: {text}")

    def _process_audio_realtime(self, audio_data: bytes, duration_ms: int,
                                forced_mode: Optional[str],
                                rt_future: concurrent.futures.Future,
                                language: Optional[str]):
        """Wait for the realtime WS session to deliver a committed transcript,
        then run the same post-STT pipeline as batch.

        On ANY realtime failure (RealtimeError or future timeout) we save the
        in-memory WAV to ``pending/`` so the 45-second scheduler worker will
        retry via batch — exact same fallback as ``TranscriptionError``. No
        audio is ever lost.
        """
        proc_t0 = time.monotonic()  # C5: release→inserted pipeline clock
        self._last_audio_data = audio_data
        self._last_duration_ms = duration_ms
        with self._lock:
            self._is_processing = True
        try:
            try:
                # The transcribe coroutine includes its own commit grace
                # timeout (~8 s), but we wrap concurrently so a hung WS can
                # never wedge the processing thread forever.
                text = rt_future.result(timeout=15.0)
            except concurrent.futures.TimeoutError as e:
                logging.warning(
                    "Realtime future timeout, falling back to pending+batch"
                )
                rt_future.cancel()
                self._queue_for_batch_retry(
                    audio_data, duration_ms, language,
                    title="VoiceDrop — realtime таймаут",
                    reason="Realtime-распознавание не успело. Дошлём через очередь.",
                )
                return
            except RealtimeError as e:
                # commit_throttled with nothing committed on a sub-0.3s clip =
                # no speech. Discard it (short beep, NO pending) — queueing it
                # for batch is exactly what created the audio_too_short poison
                # loop in the pending queue.
                if getattr(e, "throttled_empty", False) and duration_ms < 300:
                    logging.info(
                        "Realtime commit_throttled on %d ms clip — discarding "
                        "as too short (no pending)", duration_ms
                    )
                    winsound.Beep(400, 120)  # brief "too short" beep
                    if self.recording_overlay and self._root:
                        self._root.after(0, self.recording_overlay.hide)
                    return
                logging.warning(f"Realtime failed ({e}); falling back to pending+batch")
                if e.retryable:
                    self._queue_for_batch_retry(
                        audio_data, duration_ms, language,
                        title="VoiceDrop — realtime",
                        reason=(
                            "Нет сети для realtime — дошлём через очередь."
                            if e.offline else
                            "Real-time не сработал — отправим через очередь."
                        ),
                    )
                else:
                    # Non-retryable (e.g. 401/403, input_error): batch wouldn't
                    # help. Surface the error to the user, drop the clip.
                    winsound.Beep(300, 200)
                    if self.recording_overlay and self._root:
                        self._root.after(
                            0,
                            lambda: self.recording_overlay.show_error(
                                "Realtime: нет доступа"
                            ),
                        )
                        self._root.after(2500, self.recording_overlay.hide)
                    if self.tray_icon:
                        self.tray_icon.show_notification(
                            "VoiceDrop — realtime", str(e)
                        )
                return

            text = (text or "").strip()
            if not text:
                logging.info("Realtime returned empty transcript")
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return
            logging.info(f"Realtime transcript ({duration_ms} ms): {text[:80]}...")
            # For realtime, "stt" latency is the post-release drain (streaming
            # happened during recording) — approximated by the pipeline clock
            # up to this point.
            self._finish_text(text, audio_data, duration_ms, forced_mode, language,
                              stt_mode="realtime", stt_sec=time.monotonic() - proc_t0,
                              proc_t0=proc_t0)

        except Exception as e:
            logging.error(f"Error processing realtime audio: {e}", exc_info=True)
            winsound.Beep(300, 200)
            if self.recording_overlay and self._root:
                self._root.after(0, self.recording_overlay.hide)
            if self.tray_icon:
                self.tray_icon.show_notification("VoiceDrop — ошибка", str(e))
        finally:
            with self._lock:
                self._is_processing = False

    def _queue_for_batch_retry(self, audio_data: bytes, duration_ms: int,
                               language: Optional[str], title: str, reason: str):
        """Drop the clip into ``pending/`` so the scheduler retries via batch."""
        queued = False
        try:
            queued = self.pending_queue.enqueue(
                audio_data, duration_ms, language
            ) is not None
        except Exception as e:
            logging.error(f"pending_queue.enqueue failed: {e}", exc_info=True)
        msg = reason if queued else f"{reason} (не удалось сохранить в очередь)"
        winsound.Beep(300, 200)
        if self.recording_overlay and self._root:
            self._root.after(0, lambda m=msg: self.recording_overlay.show_error(m))
            self._root.after(2500, self.recording_overlay.hide)
        if self.tray_icon:
            self.tray_icon.show_notification(title, msg)

    def _process_pending_queue(self):
        """Resend recordings that failed transcription earlier (offline / outage).

        Runs on the background scheduler. On success the text goes to history (NOT
        auto-pasted — the cursor has long moved on) plus a tray notification.
        """
        # Expire stale items first (TTL) — these are dead-lettered, not retried.
        dead_lettered = list(self.pending_queue.sweep_ttl())

        items = self.pending_queue.list_pending()
        if not items and not dead_lettered:
            return
        if items:
            logging.info(f"Pending queue: {len(items)} item(s), attempting resend...")
            self._refresh_batch_stt_options()  # B1/B2 for the resend requests
        for item in items:
            try:
                audio = self.pending_queue.read_audio(item)
            except Exception as e:
                logging.error(f"Pending item {item.get('id')} unreadable, removing: {e}")
                self.pending_queue.remove(item)
                continue

            try:
                text = self.elevenlabs_client.transcribe(audio, language=item.get('language'))
            except TranscriptionError as e:
                if e.offline:
                    logging.info("Pending resend: still offline, will retry later")
                    break  # no point trying the rest of the queue while offline
                if not e.retryable:
                    # Permanent error (400 audio_too_short / validation, 401/403
                    # auth): re-sending will NEVER succeed. Move to dead-letter so
                    # it stops burning a request every 45 s (the poison-loop bug).
                    if self.pending_queue.dead_letter(item, reason=str(e)):
                        dead_lettered.append(item)
                    continue
                # Transient (5xx / 429 / transport): keep it, retry next tick.
                # DEBUG (not INFO) so a lingering outage doesn't spam the log.
                logging.debug(f"Pending resend still failing (will retry later): {e}")
                continue
            except Exception as e:
                logging.error(f"Pending resend unexpected error: {e}", exc_info=True)
                continue

            text = (text or "").strip()
            if text:
                if self._get_save_history():
                    self.db.save_recording(text, item.get('duration_ms', 0), was_inserted=False)
                logging.info(f"Pending item recovered: {text[:50]}")
                if self.tray_icon:
                    preview = text[:60] + ('…' if len(text) > 60 else '')
                    self.tray_icon.show_notification(
                        "VoiceDrop — отложенная запись расшифрована",
                        f"{preview}\n(сохранено в Историю — скопируйте оттуда)"
                    )
            else:
                logging.info(f"Pending item {item.get('id')} transcribed empty; discarding")
            self.pending_queue.remove(item)

        # One tray notification covering everything that left the queue as a
        # permanent failure this tick (TTL sweep + non-retryable resends).
        if dead_lettered and self.tray_icon:
            n = len(dead_lettered)
            self.tray_icon.show_notification(
                "VoiceDrop — записи не удалось расшифровать",
                f"Отброшено из очереди (постоянная ошибка): {n}. "
                f"Файлы сохранены в pending/dead/."
            )

    def _apply_saved_settings(self):
        """Apply saved settings on startup"""
        settings = load_settings()

        # Apply microphone setting (resolved BY NAME at capture time)
        mic_id = settings.get('microphone_id')
        mic_name = settings.get('microphone_name')
        mic_hostapi = settings.get('microphone_hostapi')
        if mic_name or mic_id is not None:
            self.audio_recorder.set_device(mic_id, mic_name, mic_hostapi)
            idx, detail = self.audio_recorder.resolve_device_index()
            if idx is None and mic_name:
                logging.warning(f"Configured microphone NOT found at startup: {detail}")
            else:
                logging.info(f"Microphone configured -> {detail}")

        # Return hotkey VK codes for initialization
        hotkey = settings.get('hotkey')
        if hotkey and 'keys' in hotkey:
            return set(hotkey['keys'])
        return None

    def _on_settings_changed(self, settings: dict):
        """Called when settings are changed"""
        # Apply microphone setting (resolved BY NAME at capture time)
        mic_id = settings.get('microphone_id')
        mic_name = settings.get('microphone_name')
        mic_hostapi = settings.get('microphone_hostapi')
        if mic_name or mic_id is not None:
            self.audio_recorder.set_device(mic_id, mic_name, mic_hostapi)
            idx, detail = self.audio_recorder.resolve_device_index()
            if idx is None and mic_name:
                logging.warning(f"Configured microphone NOT found: {detail}")
            else:
                logging.info(f"Microphone changed -> {detail}")

        # Apply hotkey setting
        hotkey = settings.get('hotkey')
        if hotkey and 'keys' in hotkey and self.hotkey_manager:
            self.hotkey_manager.set_hotkey(hotkey['keys'])
            logging.info(f"Hotkey changed to: {hotkey.get('display', 'Unknown')}")

        # Apply history retention
        self.db.set_retention(settings.get('history_retention_hours', HISTORY_RETENTION_HOURS))

        # Apply network settings (retries, timeout)
        self._apply_network_settings()

    def _get_language(self) -> Optional[str]:
        """Current language code from settings (None = auto). Used by the tray menu."""
        return load_settings().get('language_code')

    def _set_language(self, code: Optional[str]):
        """Quick-set recognition language from the tray; applies to the next recording."""
        settings = load_settings()
        settings['language_code'] = code
        name = _LANGUAGE_NAMES.get(code, "Автоопределение")
        settings['language_name'] = name
        save_settings(settings)
        logging.info(f"Language set via tray -> {name} ({code})")
        if self.tray_icon:
            self.tray_icon.show_notification("VoiceDrop — язык", f"Язык распознавания: {name}")

    def _get_polish_enabled(self) -> bool:
        """Whether LLM polish is on (default on). Used by pipeline and tray."""
        return bool(load_settings().get('polish_enabled', True))

    def _toggle_polish(self):
        """Toggle LLM polish from the tray; applies to the next recording."""
        settings = load_settings()
        new_value = not bool(settings.get('polish_enabled', True))
        settings['polish_enabled'] = new_value
        save_settings(settings)
        logging.info(f"LLM polish toggled via tray -> {new_value}")
        if self.tray_icon:
            state = "включена" if new_value else "выключена"
            self.tray_icon.show_notification("VoiceDrop — полировка", f"LLM-полировка {state}")

    def _on_polish_repeated_failure(self, reason: str):
        """Polish failed ≥2× in a row (A5). Tell the user ONCE — the text is
        still inserted, just un-polished — and flag it in the tray menu."""
        self._polish_failure_reason = reason
        if self.tray_icon:
            self.tray_icon.show_notification(
                "VoiceDrop — полировка недоступна",
                f"Polish временно не работает: {reason}. "
                f"Текст вставляется без полировки."
            )
            self.tray_icon.update_menu()

    def _on_polish_recovered(self):
        """First successful polish after a failure run — clear the warning."""
        if self._polish_failure_reason is not None:
            self._polish_failure_reason = None
            logging.info("Polish recovered; clearing tray warning")
            if self.tray_icon:
                self.tray_icon.update_menu()

    def _get_polish_status(self) -> Optional[str]:
        """None when polish is healthy; else the last failure reason (tray mark)."""
        return self._polish_failure_reason

    def _get_mode(self) -> str:
        """Dictation mode setting: 'auto' | 'text' | 'code' (default auto)."""
        return load_settings().get('dictation_mode', 'auto')

    def _effective_mode(self) -> str:
        """Resolve the setting to a concrete 'text' or 'code' for this recording."""
        return self.profiles.effective_mode(self._get_mode())

    def _set_mode(self, mode: str):
        """Set dictation mode from the tray; applies to the next recording."""
        settings = load_settings()
        settings['dictation_mode'] = mode
        save_settings(settings)
        names = {'auto': 'Авто', 'text': 'Текст', 'code': 'Код'}
        logging.info(f"Dictation mode set via tray -> {mode}")
        if self.tray_icon:
            self.tray_icon.show_notification("VoiceDrop — режим", f"Режим: {names.get(mode, mode)}")

    def _apply_network_settings(self):
        """Apply user-tunable ElevenLabs network settings (retries, read timeout)."""
        s = load_settings()
        self.elevenlabs_client.configure(
            max_retries=s.get('max_retries'),
            read_timeout=s.get('request_timeout_sec'),
        )

    # --- STT quality options (B1 no_verbatim / B2 keyterms) ------------------

    def _get_no_verbatim(self) -> bool:
        """Whether to ask Scribe to drop fillers/false-starts (B1, default on)."""
        return bool(load_settings().get('stt_no_verbatim', True))

    def _get_keyterms(self, realtime: bool) -> list:
        """Keyterm biasing list (B2), or [] when disabled. Hot-reloads the file.

        Realtime caps at 50×20, batch at 1000×50 (ElevenLabs limits)."""
        if not bool(load_settings().get('keyterms_enabled', False)):
            return []
        if realtime:
            return self.key_terms.get(max_terms=50, max_len=20)
        return self.key_terms.get(max_terms=1000, max_len=50)

    def _refresh_batch_stt_options(self):
        """Push current no_verbatim + keyterms onto the batch client. Called
        before each batch transcription so settings.json / keyterms.json edits
        take effect on the next clip without a restart."""
        self.elevenlabs_client.no_verbatim = self._get_no_verbatim()
        self.elevenlabs_client.keyterms = self._get_keyterms(realtime=False)

    def _get_polish_min_words(self) -> int:
        """Skip polish for clips shorter than this many words (C2, default 8)."""
        try:
            return max(0, int(load_settings().get('polish_min_words', 8)))
        except (TypeError, ValueError):
            return 8

    def _stt_rates(self) -> tuple:
        """(cost_batch, cost_realtime) per hour from settings (B4 defaults)."""
        s = load_settings()

        def _f(key: str, dflt: float) -> float:
            try:
                return float(s.get(key, dflt))
            except (TypeError, ValueError):
                return dflt

        return _f('stt_cost_batch', 0.22), _f('stt_cost_realtime', 0.39)

    def _get_save_history(self) -> bool:
        """Whether transcriptions are saved to the history DB (default on)."""
        return bool(load_settings().get('save_history', True))

    def _toggle_save_history(self):
        """Toggle history saving from the tray."""
        settings = load_settings()
        new_value = not bool(settings.get('save_history', True))
        settings['save_history'] = new_value
        save_settings(settings)
        logging.info(f"Save history toggled via tray -> {new_value}")
        if self.tray_icon:
            state = "включено" if new_value else "выключено"
            self.tray_icon.show_notification("VoiceDrop — история", f"Сохранение истории {state}")

    def _get_autostart(self) -> bool:
        """Whether VoiceDrop launches on Windows login (for the tray checkmark)."""
        return autostart.is_enabled()

    def _toggle_autostart(self):
        """Toggle launch-on-login from the tray."""
        new_value = not autostart.is_enabled()
        ok = autostart.set_enabled(new_value)
        if self.tray_icon:
            if ok:
                state = "включён" if new_value else "выключен"
                self.tray_icon.show_notification("VoiceDrop — автозапуск", f"Автозапуск {state}")
            else:
                self.tray_icon.show_notification("VoiceDrop — автозапуск", "Не удалось изменить")

    def _get_insert_mode(self) -> str:
        """How transcribed text is delivered: 'window' | 'clipboard' | 'enter'."""
        return load_settings().get('insert_mode', 'window')

    def _set_insert_mode(self, mode: str):
        """Set insert mode from the tray; applies to the next recording."""
        settings = load_settings()
        settings['insert_mode'] = mode
        save_settings(settings)
        names = {'window': 'В окно', 'clipboard': 'Только в буфер', 'enter': 'В окно + Enter'}
        logging.info(f"Insert mode set via tray -> {mode}")
        if self.tray_icon:
            self.tray_icon.show_notification("VoiceDrop — вставка", names.get(mode, mode))

    def _copy_last(self):
        """Copy the last produced text to the clipboard (tray quick action)."""
        if self._last_text:
            self.text_inserter.copy_to_clipboard(self._last_text)
            logging.info("Copied last text to clipboard")
            if self.tray_icon:
                preview = self._last_text[:60] + ('…' if len(self._last_text) > 60 else '')
                self.tray_icon.show_notification("VoiceDrop — скопировано", preview)
        elif self.tray_icon:
            self.tray_icon.show_notification("VoiceDrop", "Нет последнего текста")

    def _show_usage(self):
        """Show ElevenLabs STT usage (requests, audio minutes, estimated cost),
        split by batch vs realtime since they bill at different rates (B4)."""
        cb, cr = self._stt_rates()
        s = self.usage.summary(cost_batch=cb, cost_realtime=cr)
        msg = (
            f"Сегодня: {s['today_requests']} зап., {s['today_min']:.1f} мин (≈${s['today_cost']:.3f})\n"
            f"Всего: {s['total_requests']} зап., {s['total_min']:.1f} мин (≈${s['total_cost']:.2f})\n"
            f"  батч {s['total_min_batch']:.1f} мин @${cb:g}/ч · "
            f"realtime {s['total_min_realtime']:.1f} мин @${cr:g}/ч"
        )
        logging.info("Usage summary requested: " + msg.replace("\n", " | "))
        if self.tray_icon:
            self.tray_icon.show_notification("VoiceDrop — расход ElevenLabs", msg)

    def _get_pending_count(self) -> int:
        """Number of recordings waiting in the offline resend queue."""
        try:
            return self.pending_queue.count()
        except Exception:
            return 0

    def _flush_pending(self):
        """Force an immediate attempt to resend the pending queue (tray action)."""
        if self._get_pending_count() == 0:
            if self.tray_icon:
                self.tray_icon.show_notification("VoiceDrop — очередь", "Очередь пуста")
            return
        threading.Thread(target=self._process_pending_queue, daemon=True).start()

    def _retranscribe_last(self, language: str):
        """Re-transcribe the last recording with a forced language (RU/UK/EN bleed fix)."""
        audio = self._last_audio_data
        if not audio:
            if self.tray_icon:
                self.tray_icon.show_notification("VoiceDrop", "Нет последней записи для переписывания")
            return
        threading.Thread(
            target=self._do_retranscribe, args=(audio, self._last_duration_ms, language),
            daemon=True
        ).start()

    def _do_retranscribe(self, audio: bytes, duration_ms: int, language: str):
        names = {'ru': 'RU', 'uk': 'UK', 'en': 'EN'}
        try:
            logging.info(f"Re-transcribing last recording, forced language={language}")
            self._refresh_batch_stt_options()  # B1/B2
            text = self.elevenlabs_client.transcribe(audio, language=language)
            text = (text or "").strip()
            if not text:
                if self.tray_icon:
                    self.tray_icon.show_notification("VoiceDrop", "Переписывание дало пустой текст")
                return
            # Deterministic passes only (dictionary + commands); language is forced.
            text = self.text_replacer.apply(text)
            text = self.voice_commands.apply(text)
            self._last_text = text
            self.text_inserter.copy_to_clipboard(text)
            if self._get_save_history():
                self.db.save_recording(text, duration_ms, was_inserted=False)
            self.usage.record(duration_ms, len(text))
            winsound.Beep(800, 100)
            logging.info(f"Re-transcribed ({language}): {text[:50]}")
            if self.tray_icon:
                preview = text[:60] + ('…' if len(text) > 60 else '')
                self.tray_icon.show_notification(
                    f"VoiceDrop — переписано ({names.get(language, language)})",
                    f"{preview}\n(в буфере — вставьте Ctrl+V)"
                )
        except TranscriptionError as e:
            logging.error(f"Re-transcribe failed: {e}")
            if self.tray_icon:
                self.tray_icon.show_notification("VoiceDrop — ошибка", str(e))
        except Exception as e:
            logging.error(f"Re-transcribe error: {e}", exc_info=True)

    def _show_history(self):
        """Show history window"""
        if self._root:
            self._root.after(0, self._show_history_safe)

    def _show_history_safe(self):
        """Thread-safe show history"""
        if self.history_window is None:
            self.history_window = HistoryWindow(
                self._root,
                on_copy_callback=self.text_inserter.copy_to_clipboard,
                usage_provider=self._usage_snapshot,
                balance_provider=self.elevenlabs_client.get_subscription,
            )
        self.history_window.show()

    def _usage_snapshot(self) -> dict:
        """Today + month-to-date local usage, evaluated each refresh so a live
        change to the batch/realtime rates takes effect immediately (B4)."""
        cb, cr = self._stt_rates()
        s = self.usage.summary(cost_batch=cb, cost_realtime=cr)
        m = self.usage.month_summary(cost_batch=cb, cost_realtime=cr)
        return {**s, **m, "cost_batch": cb, "cost_realtime": cr}

    def _show_settings(self):
        """Show settings window"""
        if self._root:
            self._root.after(0, self._show_settings_safe)

    def _show_settings_safe(self):
        """Thread-safe show settings"""
        if self.settings_window is None:
            self.settings_window = SettingsWindow(
                self._root,
                audio_recorder=self.audio_recorder,
                on_settings_changed=self._on_settings_changed
            )
        self.settings_window.show()

    def _on_quit(self):
        """Handle application quit"""
        if self._is_shutting_down:
            return
        self._is_shutting_down = True

        logging.info("Shutting down...")

        # Force unmute all audio before exit
        try:
            self.audio_muter.force_unmute_all()
        except Exception as e:
            logging.error(f"Error unmuting audio: {e}")

        if self.hotkey_manager:
            try:
                self.hotkey_manager.stop()
            except Exception as e:
                logging.error(f"Error stopping hotkey manager: {e}")

        if self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception as e:
                logging.error(f"Error stopping scheduler: {e}")

        try:
            self.elevenlabs_client.close()
        except Exception as e:
            logging.error(f"Error closing ElevenLabs client: {e}")

        try:
            self.text_polisher.close()
        except Exception as e:
            logging.error(f"Error closing text polisher: {e}")

        # Tear down the realtime asyncio loop (no-op if never started).
        try:
            self._stop_rt_loop()
        except Exception as e:
            logging.error(f"Error stopping realtime loop: {e}")

        try:
            self.db.close()
        except Exception as e:
            logging.error(f"Error closing database: {e}")

        if self.recording_overlay:
            try:
                self.recording_overlay.destroy()
            except Exception as e:
                logging.error(f"Error destroying overlay: {e}")

        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception as e:
                logging.error(f"Error destroying root window: {e}")

        logging.info("Shutdown complete")

    def run(self):
        """Run the application"""
        # Check API key
        if not ELEVENLABS_API_KEY:
            logging.error("ELEVENLABS_API_KEY environment variable is not set!")
            logging.error("Please set it before running the application:")
            logging.error("  set ELEVENLABS_API_KEY=your_api_key_here")
            sys.exit(1)

        # Get hotkey display name
        settings = load_settings()
        hotkey_display = settings.get('hotkey', {}).get('display', 'Ctrl + Shift + Space')

        logging.info(f"Starting VoiceDrop...")
        logging.info(f"Hotkey: {hotkey_display}")
        logging.info(f"Hold to record, release to transcribe")
        logging.info(f"Log file: {LOG_FILE}")

        # Initialize customtkinter
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._root = ctk.CTk()
        self._root.withdraw()  # Hide root window

        # Set icon for root window (will be inherited by child windows)
        icon_path = os.path.join(APP_DIR, 'assets', 'icon.ico')
        if os.path.exists(icon_path):
            self._root.iconbitmap(icon_path)

        # Create recording overlay
        self.recording_overlay = RecordingOverlay()

        # Connect audio callbacks for real-time visualization
        self.recording_overlay.set_audio_callbacks(
            get_level=self.audio_recorder.get_current_level,
            get_duration=self.audio_recorder.get_recording_duration
        )

        # Start scheduler
        self.scheduler.start()

        # Setup hotkey manager with saved VK codes
        self.hotkey_manager = HotkeyManager(
            on_press_callback=self._on_hotkey_press,
            on_release_callback=self._on_hotkey_release,
            hotkey_vks=self._saved_hotkey_vks,
            modifier_vks=self._code_modifier_vks
        )
        self.hotkey_manager.start()

        # Create tray icon
        self.tray_icon = TrayIcon(
            on_show_history=self._show_history,
            on_quit=self._on_quit,
            on_settings=self._show_settings,
            on_set_language=self._set_language,
            get_language=self._get_language,
            on_toggle_polish=self._toggle_polish,
            get_polish_enabled=self._get_polish_enabled,
            get_polish_status=self._get_polish_status,
            on_set_mode=self._set_mode,
            get_mode=self._get_mode,
            on_retranscribe=self._retranscribe_last,
            on_copy_last=self._copy_last,
            on_show_usage=self._show_usage,
            on_set_insert_mode=self._set_insert_mode,
            get_insert_mode=self._get_insert_mode,
            on_toggle_autostart=self._toggle_autostart,
            get_autostart=self._get_autostart,
            on_toggle_save_history=self._toggle_save_history,
            get_save_history=self._get_save_history,
            get_pending_count=self._get_pending_count,
            on_flush_pending=self._flush_pending,
            app_version=APP_VERSION
        )

        # Run tray icon in separate thread (it blocks)
        tray_thread = threading.Thread(target=self.tray_icon.start, daemon=True)
        tray_thread.start()

        # Run tkinter mainloop (needed for windows)
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            self._on_quit()


def main():
    app = VoiceDropApp()
    app.run()


if __name__ == "__main__":
    main()
