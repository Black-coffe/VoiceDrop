"""
Audio Muter - Mutes system audio during recording
Uses Windows Audio Session API via pycaw

All pycaw/COM work runs on ONE long-lived thread that initializes COM once and
owns every COM pointer it creates. Callers on any thread go through
``_run_on_com_thread``. Before this, each press muted from a fresh short-lived
thread with no CoInitialize of its own; its COM pointers could be released
later from another thread / apartment, which crashed the process with an
access violation (0xc0000005, "unknown" faulting module) — 2026-10-05.
"""
from typing import Callable, Dict, List, Optional, Tuple, Set
import json
import queue
import threading
import logging

from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

from config import BASE_DIR


# How long a caller waits for the COM thread. On timeout the job still runs
# later, in FIFO order — so a late mute can never land after the unmute that
# was queued behind it.
_COM_CALL_TIMEOUT_SEC = 5.0


class AudioMuter:
    # Default apps never muted while recording (editable via mute_whitelist.json)
    DEFAULT_WHITELIST = {
        'chrome.exe',
        'firefox.exe',
        'msedge.exe',
        'opera.exe',
        'brave.exe',
        'discord.exe',
        'slack.exe',
        'teams.exe',
        'zoom.exe',
        'skype.exe',
        'telegram.exe',
        'whatsapp.exe',
    }

    def __init__(self):
        self._saved_states: Dict[int, Tuple[float, bool]] = {}  # pid -> (volume, was_muted)
        self._lock = threading.Lock()
        self._is_muted = False
        self._muted_pids: Set[int] = set()  # Track which PIDs we actually muted
        # Editable, hot-reloaded whitelist file (apps NOT to mute)
        self._whitelist_path = BASE_DIR / "mute_whitelist.json"
        self._whitelist_raw: Optional[str] = None
        self._whitelist: Set[str] = set(self.DEFAULT_WHITELIST)
        if not self._whitelist_path.exists():
            self._seed_whitelist()
        self._load_whitelist()
        # Dedicated COM thread (started lazily on first call).
        self._com_jobs: "queue.Queue" = queue.Queue()
        self._com_thread: Optional[threading.Thread] = None
        self._com_thread_lock = threading.Lock()

    # --- COM thread -----------------------------------------------------------
    def _com_thread_main(self):
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception as e:
            # Keep serving jobs anyway: pycaw calls will fail and be logged
            # by the impls, which is no worse than not muting.
            logging.error(f"COM init failed on audio-muter thread: {e}")
        # Never CoUninitialize: the thread lives as long as the process, so no
        # COM pointer created here can outlive its apartment.
        while True:
            fn, done, box = self._com_jobs.get()
            try:
                box.append(fn())
            except Exception as e:
                logging.error(f"Audio muter job failed: {e}", exc_info=True)
                box.append(False)
            finally:
                done.set()
                # Drop our refs so nothing COM-related lingers past the job.
                fn = done = box = None

    def _run_on_com_thread(self, fn: Callable[[], bool]) -> bool:
        if threading.current_thread() is self._com_thread:
            return fn()
        with self._com_thread_lock:
            if self._com_thread is None or not self._com_thread.is_alive():
                self._com_thread = threading.Thread(
                    target=self._com_thread_main, daemon=True, name="audio-muter-com"
                )
                self._com_thread.start()
        done = threading.Event()
        box: List[bool] = []
        self._com_jobs.put((fn, done, box))
        if not done.wait(_COM_CALL_TIMEOUT_SEC):
            logging.warning(
                f"Audio muter: {getattr(fn, '__name__', fn)} still running after "
                f"{_COM_CALL_TIMEOUT_SEC:g}s; it will finish in the background"
            )
            return False
        return box[0]

    def _seed_whitelist(self):
        payload = {
            "_comment": ("Приложения, которые НЕ глушить во время записи (имена "
                         "процессов в нижнем регистре, с .exe). Файл подхватывается "
                         "на лету. Пустой список = глушить всё."),
            "whitelist": sorted(self.DEFAULT_WHITELIST),
        }
        try:
            with open(self._whitelist_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logging.info(f"Seeded mute whitelist: {self._whitelist_path}")
        except Exception as e:
            logging.error(f"Could not seed mute whitelist: {e}")

    def _load_whitelist(self):
        """Reload the whitelist if mute_whitelist.json content changed."""
        raw: Optional[str] = None
        if self._whitelist_path.exists():
            try:
                with open(self._whitelist_path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception as e:
                logging.error(f"Failed to read mute_whitelist.json: {e}")
        if raw == self._whitelist_raw:
            return
        self._whitelist_raw = raw
        wl: Set[str] = set(self.DEFAULT_WHITELIST)
        if raw is not None:
            try:
                data = json.loads(raw)
                items = data.get("whitelist", []) if isinstance(data, dict) else []
                wl = {str(x).strip().lower() for x in items if str(x).strip()}
            except Exception as e:
                logging.error(f"Failed to parse mute_whitelist.json: {e}")
                wl = set(self.DEFAULT_WHITELIST)
        self._whitelist = wl
        logging.info(f"Loaded {len(self._whitelist)} mute-whitelist app(s)")

    def _get_audio_sessions(self) -> List:
        """Get all active audio sessions"""
        try:
            sessions = AudioUtilities.GetAllSessions()
            return [s for s in sessions if s.Process is not None]
        except Exception as e:
            logging.error(f"Error getting audio sessions: {e}")
            return []

    def _is_whitelisted(self, process_name: str) -> bool:
        """Check if a process should not be muted"""
        return process_name.lower() in self._whitelist

    def mute_all(self) -> bool:
        return self._run_on_com_thread(self._mute_all_impl)

    def _mute_all_impl(self) -> bool:
        """
        Mute all active audio sessions (except whitelisted apps) and save their states

        Returns:
            True if successful
        """
        self._load_whitelist()
        with self._lock:
            if self._is_muted:
                return True

            try:
                self._saved_states.clear()
                self._muted_pids.clear()
                sessions = self._get_audio_sessions()

                muted_count = 0
                skipped_count = 0

                for session in sessions:
                    try:
                        pid = session.Process.pid
                        process_name = session.Process.name()

                        # Skip whitelisted processes
                        if self._is_whitelisted(process_name):
                            logging.debug(f"Skipping whitelisted process: {process_name} (PID {pid})")
                            skipped_count += 1
                            continue

                        volume = session._ctl.QueryInterface(ISimpleAudioVolume)

                        # Save current state
                        current_volume = volume.GetMasterVolume()
                        current_mute = volume.GetMute()

                        self._saved_states[pid] = (current_volume, current_mute)

                        # Mute the session
                        volume.SetMute(True, None)
                        self._muted_pids.add(pid)
                        muted_count += 1

                        logging.debug(f"Muted: {process_name} (PID {pid})")

                    except Exception as e:
                        # Skip sessions that can't be controlled
                        logging.debug(f"Could not mute session: {e}")

                self._is_muted = True
                logging.info(f"Muted {muted_count} audio sessions, skipped {skipped_count} whitelisted apps")
                return True

            except Exception as e:
                logging.error(f"Error muting audio: {e}")
                return False

    def unmute_all(self) -> bool:
        return self._run_on_com_thread(self._unmute_all_impl)

    def _unmute_all_impl(self) -> bool:
        """
        Restore all audio sessions to their previous states
        IMPORTANT: Unmutes ALL sessions that were muted, even if process changed

        Returns:
            True if successful
        """
        self._load_whitelist()
        with self._lock:
            try:
                sessions = self._get_audio_sessions()
                restored_count = 0
                unmuted_count = 0

                for session in sessions:
                    try:
                        pid = session.Process.pid
                        process_name = session.Process.name()
                        volume = session._ctl.QueryInterface(ISimpleAudioVolume)

                        # Skip whitelisted processes (they were never muted)
                        if self._is_whitelisted(process_name):
                            continue

                        if pid in self._saved_states:
                            # Restore saved state
                            saved_volume, was_muted = self._saved_states[pid]
                            volume.SetMute(was_muted, None)
                            restored_count += 1
                            logging.debug(f"Restored: {process_name} (PID {pid}) to mute={was_muted}")
                        elif self._is_muted:
                            # No saved state but we were muting - force unmute
                            # This handles new processes that appeared during recording
                            current_mute = volume.GetMute()
                            if current_mute:
                                volume.SetMute(False, None)
                                unmuted_count += 1
                                logging.debug(f"Force unmuted: {process_name} (PID {pid})")

                    except Exception as e:
                        logging.debug(f"Could not unmute session: {e}")

                self._saved_states.clear()
                self._muted_pids.clear()
                self._is_muted = False

                total = restored_count + unmuted_count
                logging.info(f"Restored {restored_count} audio sessions, force unmuted {unmuted_count} new sessions")
                return True

            except Exception as e:
                logging.error(f"Error unmuting audio: {e}")
                self._is_muted = False
                self._saved_states.clear()
                self._muted_pids.clear()
                return False

    def force_unmute_all(self) -> bool:
        return self._run_on_com_thread(self._force_unmute_all_impl)

    def _force_unmute_all_impl(self) -> bool:
        """
        Force unmute all audio sessions regardless of saved states
        Used on startup to ensure audio is working
        Note: Only unmutes non-whitelisted apps (whitelisted apps should already be unmuted)

        Returns:
            True if successful
        """
        self._load_whitelist()
        try:
            sessions = self._get_audio_sessions()
            unmuted_count = 0
            skipped_count = 0

            for session in sessions:
                try:
                    process_name = session.Process.name()

                    # Whitelisted apps should not have been muted in the first place
                    if self._is_whitelisted(process_name):
                        skipped_count += 1
                        continue

                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    current_mute = volume.GetMute()

                    if current_mute:
                        volume.SetMute(False, None)
                        unmuted_count += 1
                        logging.debug(f"Force unmuted: {process_name}")

                except Exception as e:
                    logging.debug(f"Could not unmute session: {e}")

            logging.info(f"Force unmuted {unmuted_count} audio sessions, skipped {skipped_count} whitelisted apps")
            return True
        except Exception as e:
            logging.error(f"Error force unmuting audio: {e}")
            return False

    @property
    def is_muted(self) -> bool:
        """Check if audio is currently muted"""
        return self._is_muted
