@echo off
setlocal
cd /d "%~dp0"
set "STUDIO_UPDATE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%STUDIO_UPDATE_PYTHON%" (
    echo Open HoYo Project Studio.exe once to prepare its existing Python environment.
    echo Then run this command again to prepare a GitHub update release.
    pause
    exit /b 1
)
"%STUDIO_UPDATE_PYTHON%" -I -X utf8 "%~dp0launcher\build_update_package.py" --project-root "%~dp0." --show-package
if errorlevel 1 (
    pause
    exit /b 1
)
exit /b 0
