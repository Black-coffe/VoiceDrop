"""
VoiceDrop - Voice to Text Application
Main entry point
"""
import os
import sys
import threading
import time
import winsound
import logging
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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
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
from core.hotkey_manager import HotkeyManager
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
        self.text_inserter = TextInserter()
        self.db = DatabaseManager()
        self.db.set_retention(load_settings().get('history_retention_hours', HISTORY_RETENTION_HOURS))
        self.pending_queue = PendingQueue()
        self.text_replacer = TextReplacer()
        self.text_polisher = TextPolisher()
        self.voice_commands = VoiceCommands()
        self.profiles = ProfileManager()
        self.usage = UsageTracker()

        self.tray_icon: Optional[TrayIcon] = None
        self.history_window: Optional[HistoryWindow] = None
        self.settings_window: Optional[SettingsWindow] = None
        self.hotkey_manager: Optional[HotkeyManager] = None
        self.recording_overlay: Optional[RecordingOverlay] = None

        self._is_recording = False
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

        # Force unmute all audio on startup (in case previous instance crashed)
        logging.info("Force unmuting all audio on startup...")
        self.audio_muter.force_unmute_all()

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

    def _on_hotkey_press(self):
        """Called when hotkey is pressed - start recording.

        Capture starts FIRST so muting (slow pycaw/COM) can never delay or eat
        the recording. Muting runs in a background, race-guarded worker.
        """
        try:
            with self._lock:
                if self._is_recording:
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
            try:
                self.audio_recorder.start_recording()
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
        except Exception as e:
            logging.error(f"Unexpected error in _on_hotkey_press: {e}", exc_info=True)
            with self._lock:
                self._is_recording = False

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
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return

            # Code-mode modifier (e.g. Right Shift) held with the hotkey -> raw
            forced_mode = "code" if (self.hotkey_manager and
                                     self.hotkey_manager.modifier_was_held()) else None

            # Process in background thread
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
        # Keep this clip so it can be re-transcribed in another language later.
        self._last_audio_data = audio_data
        self._last_duration_ms = duration_ms
        try:
            # Transcribe
            logging.info("Sending to ElevenLabs...")
            start_time = time.time()

            # Get language setting (None = auto-detect)
            settings = load_settings()
            language = settings.get('language_code', None)

            text = self.elevenlabs_client.transcribe(audio_data, language=language)

            elapsed = time.time() - start_time
            logging.info(f"Transcribed in {elapsed:.2f}s: {text[:50]}...")

            if not text or not text.strip():
                logging.info("Empty transcription")
                if self.recording_overlay and self._root:
                    self._root.after(0, self.recording_overlay.hide)
                return

            text = text.strip()

            # Resolve dictation mode: hotkey modifier forces code, else the setting
            mode = forced_mode or self._effective_mode()
            if forced_mode == "code":
                logging.info("Code-mode modifier held -> raw (no polish) for this clip")

            # Optional LLM polish — text mode only (code mode stays verbatim).
            # Best-effort: returns original on error.
            if mode == "text" and self._get_polish_enabled():
                text = self.text_polisher.polish(text, language=language)
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
                self._root.after(0, lambda: self.recording_overlay.show_result(word_count, char_count))
                # Hide overlay after 1.5 seconds
                self._root.after(1500, self.recording_overlay.hide)

            self._last_text = text  # remember for "Скопировать последнее"

            # Deliver text per the chosen insert mode
            insert_mode = self._get_insert_mode()
            if insert_mode == "clipboard":
                self.text_inserter.copy_to_clipboard(text)
                inserted = False
            elif insert_mode == "enter":
                inserted = self.text_inserter.insert_text(text, press_enter=True)
            else:  # "window"
                inserted = self.text_inserter.insert_text(text)

            # Save to database (unless history saving is disabled for privacy)
            if self._get_save_history():
                self.db.save_recording(text, duration_ms, was_inserted=inserted)
            self.usage.record(duration_ms, len(text))

            # Play success sound
            winsound.Beep(800, 100)  # Short high-pitched beep

            logging.info(f"Done! Text: {text}")

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

    def _process_pending_queue(self):
        """Resend recordings that failed transcription earlier (offline / outage).

        Runs on the background scheduler. On success the text goes to history (NOT
        auto-pasted — the cursor has long moved on) plus a tray notification.
        """
        items = self.pending_queue.list_pending()
        if not items:
            return
        logging.info(f"Pending queue: {len(items)} item(s), attempting resend...")
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
                logging.info(f"Pending resend still failing (will retry later): {e}")
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
        """Show ElevenLabs STT usage (requests, audio minutes, estimated cost)."""
        rate = load_settings().get('stt_cost_per_hour', 0.40)
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 0.40
        s = self.usage.summary(cost_per_hour=rate)
        msg = (
            f"Сегодня: {s['today_requests']} зап., {s['today_min']:.1f} мин (≈${s['today_cost']:.3f})\n"
            f"Всего: {s['total_requests']} зап., {s['total_min']:.1f} мин (≈${s['total_cost']:.2f})\n"
            f"Ставка ≈${rate:g}/час (stt_cost_per_hour в settings.json)"
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
        """Today + month-to-date local usage, evaluated each refresh so a
        live setting change to stt_cost_per_hour takes effect immediately."""
        rate = load_settings().get('stt_cost_per_hour', 0.40)
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 0.40
        s = self.usage.summary(cost_per_hour=rate)
        m = self.usage.month_summary(cost_per_hour=rate)
        return {**s, **m, "cost_per_hour": rate}

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
