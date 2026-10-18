@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\launcher\create_desktop_shortcut.ps1" -ProjectRoot "%CD%"
if errorlevel 1 (
    echo.
    echo [ERROR] The shortcut could not be created.
    pause
    exit /b 1
)
echo.
pause
