@echo off
cd /d "%~dp0"
echo.
echo Starting Audio Diagnostic Tool...
echo.
".venv\Scripts\python.exe" diagnose_audio.py
pause
