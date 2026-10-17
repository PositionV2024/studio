@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build HoYo Project Studio EXE

if not exist ".venv\Scripts\python.exe" (
    echo The project environment does not exist yet.
    echo Running setup first...
    call "Setup HoYo Project Studio.cmd"
    if errorlevel 1 exit /b %errorlevel%
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\launcher\build_exe.ps1" -ProjectRoot "%CD%"
if errorlevel 1 (
    echo.
    echo [ERROR] The executable build failed.
    pause
    exit /b 1
)

echo.
pause
exit /b 0
