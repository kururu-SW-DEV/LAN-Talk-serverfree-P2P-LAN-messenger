@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "LAN Talk" pythonw.exe lan_messenger.py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        start "LAN Talk" python.exe lan_messenger.py
    ) else (
        echo Python not found. Install Python 3.10+ first.
        pause
    )
)
