"""
Emergency script to force unmute all audio sessions
Run this if sound is stuck muted
"""
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume

# These apps should never have been muted (browsers, communication)
WHITELIST_PROCESSES = {
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

def force_unmute_all():
    """Force unmute all active audio sessions"""
    try:
        sessions = AudioUtilities.GetAllSessions()
        unmuted_count = 0

        print("\n" + "=" * 60)
        print("Current Audio Sessions:")
        print("=" * 60)

        for session in sessions:
            if session.Process is not None:
                try:
                    process_name = session.Process.name()
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    is_muted = volume.GetMute()
                    current_volume = volume.GetMasterVolume()

                    status = "MUTED" if is_muted else "UNMUTED"
                    print(f"  {process_name:<30} | Volume: {current_volume:.0%} | {status}")

                    # EMERGENCY MODE: Unmute EVERYTHING (ignore whitelist)
                    if is_muted:
                        volume.SetMute(False, None)
                        unmuted_count += 1
                        print(f"    -> UNMUTED {process_name}")

                except Exception as e:
                    print(f"  Error processing session: {e}")

        print("=" * 60)
        print(f"\nResults:")
        print(f"  - Unmuted: {unmuted_count} sessions (forced ALL to unmute)")
        print("=" * 60)
        return True
    except Exception as e:
        print(f"[ForceUnmute] Error: {e}")
        return False

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Emergency Audio Unmute Tool")
    print("=" * 60)
    print("This will unmute all system audio that may have been")
    print("stuck muted by VoiceDrop or other applications.")
    print()

    force_unmute_all()

    print("\nDone! Check if your audio is working now.")
    print("If Chrome/Firefox still has no sound, try restarting the browser.")
