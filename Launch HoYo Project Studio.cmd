@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo HoYo Project Studio has not been set up yet.
    echo Starting first-time setup...
    echo.
    call "Setup HoYo Project Studio.cmd"
    exit /b %errorlevel%
)

start "HoYo Project Studio" /D "%CD%" ".venv\Scripts\pythonw.exe" "launcher\launcher.pyw"
exit /b 0
