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
LOG_FILE = os.path.join(os.path.dirname(__file__), 'voicedrop.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Fix Windows console encoding for Russian text
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    import ctypes

    # Single instance check using Windows mutex
    # Use full path to make mutex unique to this installation
    MUTEX_NAME = f"Global\\VoiceDrop_{os.path.abspath(__file__).replace(':', '').replace('\\', '_')}"

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

from config import ELEVENLABS_API_KEY
from core.audio_muter import AudioMuter
from core.audio_recorder import AudioRecorder
from core.db_manager import DatabaseManager
from core.elevenlabs_api import ElevenLabsClient
from core.hotkey_manager import HotkeyManager
from core.text_inserter import TextInserter
from gui.history_window import HistoryWindow
from gui.recording_overlay import RecordingOverlay
from gui.settings_window import SettingsWindow, load_settings
from gui.tray_icon import TrayIcon


class VoiceDropApp:
    def __init__(self):
        logging.info("Initializing VoiceDropApp...")
        self.audio_recorder = AudioRecorder()
        self.audio_muter = AudioMuter()
        self.elevenlabs_client = ElevenLabsClient()
        self.text_inserter = TextInserter()
        self.db = DatabaseManager()

        self.tray_icon: Optional[TrayIcon] = None
        self.history_window: Optional[HistoryWindow] = None
        self.settings_window: Optional[SettingsWindow] = None
        self.hotkey_manager: Optional[HotkeyManager] = None
        self.recording_overlay: Optional[RecordingOverlay] = None

        self._is_recording = False
        self._lock = threading.Lock()
        self._is_shutting_down = False

        # Force unmute all audio on startup (in case previous instance crashed)
        logging.info("Force unmuting all audio on startup...")
        self.audio_muter.force_unmute_all()

        # Load saved settings and get hotkey VK codes
        self._saved_hotkey_vks = self._apply_saved_settings()

        # Background scheduler for cleanup
        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(self.db.cleanup_old_recordings, 'interval', hours=1)

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
        """Called when hotkey is pressed - start recording"""
        with self._lock:
            if self._is_recording:
                return
            self._is_recording = True

        logging.info("Recording started...")

        # Play start recording sound (before muting)
        winsound.Beep(600, 50)

        # Mute all system audio
        self.audio_muter.mute_all()

        if self.tray_icon:
            self.tray_icon.set_recording(True)

        # Show recording overlay
        if self._root and self.recording_overlay:
            self._root.after(0, lambda: self.recording_overlay.show(self._root))

        self.audio_recorder.start_recording()

    def _on_hotkey_release(self):
        """Called when hotkey is released - stop recording and process"""
        with self._lock:
            if not self._is_recording:
                return
            self._is_recording = False

        logging.info("Recording stopped, processing...")

        # Unmute all system audio
        self.audio_muter.unmute_all()

        if self.tray_icon:
            self.tray_icon.set_recording(False)

        # Show processing state on overlay
        if self.recording_overlay:
            self._root.after(0, self.recording_overlay.show_processing)

        # Stop recording
        audio_data, duration_ms = self.audio_recorder.stop_recording()

        if not audio_data or duration_ms < 200:  # Too short
            logging.info("Recording too short, ignoring")
            return

        # Process in background thread
        threading.Thread(
            target=self._process_audio,
            args=(audio_data, duration_ms),
            daemon=True
        ).start()

    def _process_audio(self, audio_data: bytes, duration_ms: int):
        """Process audio: transcribe and insert text"""
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

            # Show word count on overlay
            word_count = len(text.split())
            char_count = len(text)
            if self.recording_overlay and self._root:
                self._root.after(0, lambda: self.recording_overlay.show_result(word_count, char_count))
                # Hide overlay after 1.5 seconds
                self._root.after(1500, self.recording_overlay.hide)

            # Copy to clipboard
            self.text_inserter.copy_to_clipboard(text)

            # Insert at cursor
            inserted = self.text_inserter.insert_text(text)

            # Save to database
            self.db.save_recording(text, duration_ms, was_inserted=inserted)

            # Play success sound
            winsound.Beep(800, 100)  # Short high-pitched beep

            logging.info(f"Done! Text: {text}")

        except Exception as e:
            logging.error(f"Error processing audio: {e}", exc_info=True)
            # Play error sound
            winsound.Beep(300, 200)  # Low-pitched error beep
            if self.recording_overlay and self._root:
                self._root.after(0, self.recording_overlay.hide)
            if self.tray_icon:
                self.tray_icon.show_notification("Ошибка", str(e))

    def _apply_saved_settings(self):
        """Apply saved settings on startup"""
        settings = load_settings()

        # Apply microphone setting
        mic_id = settings.get('microphone_id')
        if mic_id is not None:
            self.audio_recorder.set_device(mic_id)
            logging.info(f"Using microphone: {settings.get('microphone_name', mic_id)}")

        # Return hotkey VK codes for initialization
        hotkey = settings.get('hotkey')
        if hotkey and 'keys' in hotkey:
            return set(hotkey['keys'])
        return None

    def _on_settings_changed(self, settings: dict):
        """Called when settings are changed"""
        # Apply microphone setting
        mic_id = settings.get('microphone_id')
        if mic_id is not None:
            self.audio_recorder.set_device(mic_id)
            logging.info(f"Microphone changed to: {settings.get('microphone_name', mic_id)}")

        # Apply hotkey setting
        hotkey = settings.get('hotkey')
        if hotkey and 'keys' in hotkey and self.hotkey_manager:
            self.hotkey_manager.set_hotkey(hotkey['keys'])
            logging.info(f"Hotkey changed to: {hotkey.get('display', 'Unknown')}")

    def _show_history(self):
        """Show history window"""
        if self._root:
            self._root.after(0, self._show_history_safe)

    def _show_history_safe(self):
        """Thread-safe show history"""
        if self.history_window is None:
            self.history_window = HistoryWindow(
                self._root,
                on_copy_callback=self.text_inserter.copy_to_clipboard
            )
        self.history_window.show()

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
        icon_path = os.path.join(os.path.dirname(__file__), 'assets', 'icon.ico')
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
            hotkey_vks=self._saved_hotkey_vks
        )
        self.hotkey_manager.start()

        # Create tray icon
        self.tray_icon = TrayIcon(
            on_show_history=self._show_history,
            on_quit=self._on_quit,
            on_settings=self._show_settings
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
