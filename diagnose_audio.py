"""
Audio Sessions Diagnostic Tool
Shows the status of all audio sessions in the system
"""
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
import time
import sys

# These apps should never be muted by VoiceDrop
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


def show_audio_sessions():
    """Display all audio sessions and their mute status"""
    try:
        sessions = AudioUtilities.GetAllSessions()

        print("\n" + "=" * 80)
        print("AUDIO SESSIONS STATUS")
        print("=" * 80)
        print(f"{'Process Name':<30} | {'PID':<8} | {'Volume':<8} | {'Status':<10} | {'Protected'}")
        print("-" * 80)

        muted_apps = []
        whitelisted_apps = []

        for session in sessions:
            if session.Process is not None:
                try:
                    process_name = session.Process.name()
                    pid = session.Process.pid
                    volume_ctrl = session._ctl.QueryInterface(ISimpleAudioVolume)
                    is_muted = volume_ctrl.GetMute()
                    current_volume = volume_ctrl.GetMasterVolume()

                    status = "🔇 MUTED" if is_muted else "🔊 UNMUTED"
                    is_protected = "✓ YES" if process_name.lower() in WHITELIST_PROCESSES else "  NO"

                    print(f"{process_name:<30} | {pid:<8} | {current_volume:>6.0%} | {status:<10} | {is_protected}")

                    if is_muted:
                        muted_apps.append(process_name)
                    if process_name.lower() in WHITELIST_PROCESSES:
                        whitelisted_apps.append((process_name, is_muted))

                except Exception as e:
                    print(f"  Error reading session: {e}")

        print("=" * 80)
        print(f"\nSummary:")
        print(f"  Total sessions: {len([s for s in sessions if s.Process is not None])}")
        print(f"  Muted apps: {len(muted_apps)}")
        print(f"  Whitelisted apps: {len(whitelisted_apps)}")

        if muted_apps:
            print(f"\n⚠️  Currently muted apps:")
            for app in muted_apps:
                print(f"    - {app}")

        # Check for problems
        problems = []
        for app_name, is_muted in whitelisted_apps:
            if is_muted:
                problems.append(app_name)

        if problems:
            print(f"\n❌ PROBLEM DETECTED!")
            print(f"   The following protected apps are MUTED (they should NOT be muted):")
            for app in problems:
                print(f"    - {app}")
            print(f"\n   This is likely what's causing your audio issues!")
            print(f"   Run 'force_unmute.bat' to fix this.")

        print("\n" + "=" * 80)
        return True

    except Exception as e:
        print(f"Error: {e}")
        return False


def monitor_audio_sessions():
    """Monitor audio sessions in real-time"""
    print("\n" + "=" * 80)
    print("REAL-TIME AUDIO SESSION MONITOR")
    print("=" * 80)
    print("Monitoring audio sessions... Press Ctrl+C to stop\n")

    previous_muted = set()

    try:
        while True:
            sessions = AudioUtilities.GetAllSessions()
            current_muted = set()

            for session in sessions:
                if session.Process is not None:
                    try:
                        process_name = session.Process.name()
                        volume_ctrl = session._ctl.QueryInterface(ISimpleAudioVolume)
                        is_muted = volume_ctrl.GetMute()

                        if is_muted:
                            current_muted.add(process_name)

                    except Exception:
                        pass

            # Check for changes
            newly_muted = current_muted - previous_muted
            newly_unmuted = previous_muted - current_muted

            if newly_muted:
                for app in newly_muted:
                    timestamp = time.strftime("%H:%M:%S")
                    protected = "⚠️ PROTECTED APP!" if app.lower() in WHITELIST_PROCESSES else ""
                    print(f"[{timestamp}] 🔇 MUTED: {app} {protected}")

            if newly_unmuted:
                for app in newly_unmuted:
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] 🔊 UNMUTED: {app}")

            previous_muted = current_muted
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")
        return True


def main():
    print("\n" + "=" * 80)
    print("VOICEDROP AUDIO DIAGNOSTIC TOOL")
    print("=" * 80)
    print("\nOptions:")
    print("  1. Show current audio sessions status")
    print("  2. Monitor audio sessions in real-time")
    print("  3. Exit")
    print()

    while True:
        choice = input("Select option (1-3): ").strip()

        if choice == "1":
            show_audio_sessions()
            input("\nPress Enter to continue...")
        elif choice == "2":
            monitor_audio_sessions()
            input("\nPress Enter to continue...")
        elif choice == "3":
            print("Exiting...")
            break
        else:
            print("Invalid option, please try again.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nError: {e}")
        input("\nPress Enter to exit...")
