"""
Emergency script to force unmute all audio sessions
Run this if sound is stuck muted
"""
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

def force_unmute_all():
    """Force unmute all active audio sessions"""
    try:
        sessions = AudioUtilities.GetAllSessions()
        unmuted_count = 0

        for session in sessions:
            if session.Process is not None:
                try:
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    volume.SetMute(False, None)
                    unmuted_count += 1
                except Exception:
                    pass

        print(f"[ForceUnmute] Unmuted {unmuted_count} audio sessions")
        return True
    except Exception as e:
        print(f"[ForceUnmute] Error: {e}")
        return False

if __name__ == "__main__":
    print("[ForceUnmute] Forcing all audio sessions to unmute...")
    force_unmute_all()
    print("[ForceUnmute] Done!")
