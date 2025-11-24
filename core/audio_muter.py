"""
Audio Muter - Mutes system audio during recording
Uses Windows Audio Session API via pycaw
"""
from typing import Dict, List, Optional, Tuple
import threading

from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume


class AudioMuter:
    def __init__(self):
        self._saved_states: Dict[int, Tuple[float, bool]] = {}  # pid -> (volume, was_muted)
        self._lock = threading.Lock()
        self._is_muted = False

    def _get_audio_sessions(self) -> List:
        """Get all active audio sessions"""
        try:
            sessions = AudioUtilities.GetAllSessions()
            return [s for s in sessions if s.Process is not None]
        except Exception as e:
            print(f"[AudioMuter] Error getting sessions: {e}")
            return []

    def mute_all(self) -> bool:
        """
        Mute all active audio sessions and save their states

        Returns:
            True if successful
        """
        with self._lock:
            if self._is_muted:
                return True

            try:
                self._saved_states.clear()
                sessions = self._get_audio_sessions()

                for session in sessions:
                    try:
                        volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                        pid = session.Process.pid

                        # Save current state
                        current_volume = volume.GetMasterVolume()
                        current_mute = volume.GetMute()

                        self._saved_states[pid] = (current_volume, current_mute)

                        # Mute the session
                        volume.SetMute(True, None)

                    except Exception as e:
                        # Skip sessions that can't be controlled
                        pass

                self._is_muted = True
                print(f"[AudioMuter] Muted {len(self._saved_states)} audio sessions")
                return True

            except Exception as e:
                print(f"[AudioMuter] Error muting: {e}")
                return False

    def unmute_all(self) -> bool:
        """
        Restore all audio sessions to their previous states
        If no saved states exist, force unmute all sessions

        Returns:
            True if successful
        """
        with self._lock:
            try:
                sessions = self._get_audio_sessions()
                restored_count = 0

                for session in sessions:
                    try:
                        pid = session.Process.pid
                        volume = session._ctl.QueryInterface(ISimpleAudioVolume)

                        if pid in self._saved_states:
                            # Restore saved state
                            saved_volume, was_muted = self._saved_states[pid]
                            volume.SetMute(was_muted, None)
                            restored_count += 1
                        elif self._is_muted:
                            # No saved state but we were muting - force unmute
                            volume.SetMute(False, None)
                            restored_count += 1

                    except Exception:
                        pass

                self._saved_states.clear()
                self._is_muted = False
                print(f"[AudioMuter] Restored {restored_count} audio sessions")
                return True

            except Exception as e:
                print(f"[AudioMuter] Error unmuting: {e}")
                self._is_muted = False
                return False

    def force_unmute_all(self) -> bool:
        """
        Force unmute all audio sessions regardless of saved states
        Used on startup to ensure audio is working

        Returns:
            True if successful
        """
        try:
            sessions = self._get_audio_sessions()
            unmuted_count = 0

            for session in sessions:
                try:
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    volume.SetMute(False, None)
                    unmuted_count += 1
                except Exception:
                    pass

            print(f"[AudioMuter] Force unmuted {unmuted_count} audio sessions")
            return True
        except Exception as e:
            print(f"[AudioMuter] Error force unmuting: {e}")
            return False

    @property
    def is_muted(self) -> bool:
        """Check if audio is currently muted"""
        return self._is_muted
