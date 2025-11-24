@echo off
cd /d "%~dp0"
echo.
echo ========================================
echo   EMERGENCY AUDIO UNMUTE
echo ========================================
echo.
echo This will unmute ALL system audio that may be stuck muted.
echo.
".venv\Scripts\python.exe" force_unmute.py
