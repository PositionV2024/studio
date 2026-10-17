@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title HoYo Project Studio Setup

 echo.
 echo ============================================================
 echo   HoYo Project Studio - Setup and update
 echo ============================================================
 echo.

set "BASE_PYTHON="

if exist ".venv\Scripts\python.exe" goto :existing_environment

where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=py -3.12"
)

if not defined BASE_PYTHON (
    py -3.13 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=py -3.13"
)

if not defined BASE_PYTHON (
    py -3.14 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=py -3.14"
)

if not defined BASE_PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=py"
)

if not defined BASE_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 set "BASE_PYTHON=python"
)

if not defined BASE_PYTHON (
    echo [ERROR] Python could not be found.
    echo Install Python 3.11 or newer, then run this file again.
    echo Make sure the Python Launcher ^(py^) is enabled during installation.
    echo.
    pause
    exit /b 1
)

echo [1/5] Creating the isolated application environment...
%BASE_PYTHON% -m venv .venv
if errorlevel 1 goto :failed
goto :environment_ready

:existing_environment
echo [1/5] Existing application environment found.

:environment_ready

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"

echo [2/5] Updating pip...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo [3/5] Installing HoYo Project Studio dependencies...
"%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [4/5] Checking Python code completion...
"%VENV_PYTHON%" "launcher\check_python_completion.py"
if errorlevel 1 goto :failed

echo [5/5] Creating a desktop shortcut...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\launcher\create_desktop_shortcut.ps1" -ProjectRoot "%CD%"
if errorlevel 1 (
    echo [WARNING] The app is installed, but the desktop shortcut could not be created.
)

> ".venv\.hoyo_studio_ready" echo ready

echo.
echo ============================================================
echo   Setup complete.
echo ============================================================
echo.
echo You can now double-click:
echo   Launch HoYo Project Studio.vbs
echo.
choice /C YN /N /M "Launch HoYo Project Studio now? [Y/N]: "
if errorlevel 2 exit /b 0
call "Launch HoYo Project Studio.cmd"
exit /b 0

:failed
echo.
echo [ERROR] Setup failed. Review the message above and try again.
echo.
pause
exit /b 1
