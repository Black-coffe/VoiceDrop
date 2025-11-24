"""
Diagnostic tool to monitor Python processes and detect if something is killing them.
Run this script while VoiceDrop is running to see if any processes terminate unexpectedly.
"""
import psutil
import time
import sys
from datetime import datetime


def find_python_processes():
    """Find all Python processes running on the system"""
    python_processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            name = proc.info['name'].lower()
            if 'python' in name:
                python_processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return python_processes


def format_process_info(proc):
    """Format process information for display"""
    try:
        cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else 'N/A'
        create_time = datetime.fromtimestamp(proc.info['create_time']).strftime('%H:%M:%S')
        return f"  PID {proc.info['pid']} | {proc.info['name']} | Started: {create_time}\n  Command: {cmdline}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return f"  PID {proc.info['pid']} | Access Denied"


def main():
    print("=" * 80)
    print("Python Process Monitor - Diagnostic Tool for VoiceDrop")
    print("=" * 80)
    print("\nThis tool will monitor all Python processes and alert you if any are terminated.")
    print("Leave this running while you work with other Python projects (PyCharm, etc.)\n")
    print("Press Ctrl+C to stop monitoring\n")

    # Initial scan
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Initial scan...")
    processes = {proc.info['pid']: proc for proc in find_python_processes()}

    print(f"\nFound {len(processes)} Python process(es):\n")
    for proc in processes.values():
        print(format_process_info(proc))
        print()

    # Find VoiceDrop process
    voicedrop_pids = []
    for pid, proc in processes.items():
        try:
            cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
            if 'main.py' in cmdline or 'VoiceDrop' in cmdline:
                voicedrop_pids.append(pid)
                print(f">>> VoiceDrop detected: PID {pid} <<<")
        except:
            pass

    if not voicedrop_pids:
        print("\nWARNING: VoiceDrop process not detected!")
        print("Make sure VoiceDrop is running before starting this diagnostic tool.\n")

    print("\n" + "=" * 80)
    print("Monitoring started... (checking every 2 seconds)")
    print("=" * 80 + "\n")

    try:
        while True:
            time.sleep(2)

            # Check current processes
            current_processes = {proc.info['pid']: proc for proc in find_python_processes()}

            # Check for terminated processes
            terminated = set(processes.keys()) - set(current_processes.keys())
            if terminated:
                for pid in terminated:
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    proc = processes[pid]
                    print(f"\n{'!' * 80}")
                    print(f"[{timestamp}] ALERT: Python process TERMINATED!")
                    print(f"{'!' * 80}")
                    print(format_process_info(proc))

                    if pid in voicedrop_pids:
                        print("\n>>> THIS WAS THE VOICEDROP PROCESS! <<<")
                        print("\nPossible causes:")
                        print("  1. Another program is killing Python processes")
                        print("  2. PyCharm or IDE is managing Python processes")
                        print("  3. Antivirus or security software")
                        print("  4. System script or task manager automation")
                        print("\nCheck Task Scheduler, startup scripts, or security software settings.")

                    print()

            # Check for new processes
            new_pids = set(current_processes.keys()) - set(processes.keys())
            if new_pids:
                for pid in new_pids:
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    proc = current_processes[pid]
                    print(f"\n[{timestamp}] New Python process started:")
                    print(format_process_info(proc))
                    print()

            # Update process list
            processes = current_processes

    except KeyboardInterrupt:
        print("\n\n" + "=" * 80)
        print("Monitoring stopped")
        print("=" * 80)
        sys.exit(0)


if __name__ == "__main__":
    main()
