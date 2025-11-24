@echo off
cd /d "%~dp0"
echo Starting Python process diagnostic tool...
echo.
".venv\Scripts\python.exe" diagnose_python.py
pause
