@echo off
echo Removing VoiceDrop from Windows Startup...

set SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VoiceDrop.lnk

if exist "%SHORTCUT_PATH%" (
    del "%SHORTCUT_PATH%"
    echo.
    echo SUCCESS! VoiceDrop removed from autostart.
) else (
    echo.
    echo VoiceDrop was not in autostart.
)

echo.
pause
