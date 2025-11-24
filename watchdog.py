"""
Watchdog script to monitor VoiceDrop and automatically restart it if it crashes or is killed.
This helps protect against other programs killing Python processes.
"""
import psutil
import subprocess
import time
import os
import sys
from datetime import datetime
import logging

# Setup logging
LOG_FILE = os.path.join(os.path.dirname(__file__), 'watchdog.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)


def find_voicedrop_process():
    """Find the VoiceDrop process"""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = proc.info['name'].lower()
            if 'python' in name:
                cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
                if 'main.py' in cmdline and 'VoiceDrop' in cmdline:
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def start_voicedrop():
    """Start VoiceDrop"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pythonw_exe = os.path.join(script_dir, '.venv', 'Scripts', 'pythonw.exe')
    main_py = os.path.join(script_dir, 'main.py')

    if not os.path.exists(pythonw_exe):
        logging.error(f"pythonw.exe not found at {pythonw_exe}")
        return None

    if not os.path.exists(main_py):
        logging.error(f"main.py not found at {main_py}")
        return None

    logging.info("Starting VoiceDrop...")
    try:
        # Start process without creating a window
        subprocess.Popen(
            [pythonw_exe, main_py],
            cwd=script_dir,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        time.sleep(2)  # Give it time to start
        return find_voicedrop_process()
    except Exception as e:
        logging.error(f"Failed to start VoiceDrop: {e}")
        return None


def main():
    logging.info("=" * 80)
    logging.info("VoiceDrop Watchdog Started")
    logging.info("=" * 80)
    logging.info("This watchdog will monitor VoiceDrop and restart it if it crashes.")
    logging.info("Log file: " + LOG_FILE)
    logging.info("")

    restart_count = 0
    last_restart_time = None

    # Check if VoiceDrop is already running
    proc = find_voicedrop_process()
    if proc:
        logging.info(f"VoiceDrop already running (PID {proc.info['pid']})")
    else:
        logging.info("VoiceDrop not running, starting it...")
        proc = start_voicedrop()
        if not proc:
            logging.error("Failed to start VoiceDrop. Exiting.")
            sys.exit(1)
        logging.info(f"VoiceDrop started (PID {proc.info['pid']})")

    logging.info("\nWatchdog monitoring active. Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(3)

            # Check if process still exists
            if proc and proc.is_running():
                # Process is running, all good
                continue

            # Process is not running!
            logging.warning("!" * 80)
            logging.warning("VoiceDrop process terminated!")
            logging.warning("!" * 80)

            if last_restart_time:
                time_since_restart = time.time() - last_restart_time
                logging.warning(f"Process was running for {time_since_restart:.1f} seconds")

                # If it crashed within 10 seconds, something is seriously wrong
                if time_since_restart < 10:
                    logging.error("Process crashed within 10 seconds of restart!")
                    logging.error("This suggests a critical issue. Stopping watchdog.")
                    logging.error("Check voicedrop.log for errors.")
                    sys.exit(1)

            restart_count += 1
            logging.info(f"Attempting restart #{restart_count}...")

            # Try to restart
            proc = start_voicedrop()
            last_restart_time = time.time()

            if proc:
                logging.info(f"VoiceDrop restarted successfully (PID {proc.info['pid']})")
            else:
                logging.error("Failed to restart VoiceDrop")
                logging.error("Waiting 10 seconds before next attempt...")
                time.sleep(10)

            # Alert if too many restarts
            if restart_count >= 5:
                logging.warning(f"VoiceDrop has been restarted {restart_count} times!")
                logging.warning("This suggests something is repeatedly killing the process.")
                logging.warning("Check for:")
                logging.warning("  - Task killer scripts or automation")
                logging.warning("  - Antivirus or security software")
                logging.warning("  - IDE process management")

    except KeyboardInterrupt:
        logging.info("\n" + "=" * 80)
        logging.info("Watchdog stopped by user")
        logging.info(f"Total restarts during this session: {restart_count}")
        logging.info("=" * 80)
        sys.exit(0)


if __name__ == "__main__":
    main()
