@echo off
echo Creating VoiceDrop shortcut in Windows Startup folder...

set SCRIPT_DIR=%~dp0
set STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set SHORTCUT_PATH=%STARTUP_FOLDER%\VoiceDrop.lnk

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%SCRIPT_DIR%run.bat'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.WindowStyle = 7; $s.Description = 'VoiceDrop Voice-to-Text'; $s.Save()"

if exist "%SHORTCUT_PATH%" (
    echo.
    echo SUCCESS! VoiceDrop will start automatically with Windows.
    echo Shortcut created: %SHORTCUT_PATH%
) else (
    echo.
    echo ERROR: Failed to create shortcut.
)

echo.
pause
