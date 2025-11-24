@echo off
cd /d "%~dp0"
echo Starting VoiceDrop with Watchdog protection...
echo.
echo The watchdog will automatically restart VoiceDrop if it crashes or is killed.
echo Close this window to stop the watchdog.
echo.
".venv\Scripts\python.exe" watchdog.py
pause
