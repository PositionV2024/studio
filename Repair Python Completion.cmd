@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title HoYo Project Studio - Repair Python Completion

echo.
echo ============================================================
echo   HoYo Project Studio - Repair Python Completion
echo ============================================================
echo.
echo Close HoYo Project Studio before running this repair.
echo This updates the editor helper in the Studio application's
echo existing Python environment, then tests real suggestions.
echo.

set "STUDIO_REPAIR_PYTHON=%CD%\.venv\Scripts\python.exe"
set "STUDIO_REPAIR_CHECK=%CD%\launcher\check_python_completion.py"

if not exist "%STUDIO_REPAIR_PYTHON%" goto :missing_python
if not exist "%STUDIO_REPAIR_CHECK%" goto :missing_check

"%STUDIO_REPAIR_PYTHON%" "%STUDIO_REPAIR_CHECK%" --repair
if errorlevel 1 goto :failed

echo.
echo [PASS] The completion helper is ready. Reopen HoYo Project Studio.
echo.
pause
exit /b 0

:missing_python
echo [FAIL] The Studio application's Python environment was not found.
echo Run Setup HoYo Project Studio.cmd from this application folder,
echo then run this repair again.
goto :failed

:missing_check
echo [FAIL] launcher\check_python_completion.py was not found.
echo Copy all the files from the completion fix into this application
echo folder, merging the launcher and studio folders.
goto :failed

:failed
echo.
echo [FAIL] Repair did not finish successfully. Review the details above.
echo If a diagnostic report was written, its full path is shown above.
echo Keep that report if you need help with the error.
echo.
pause
exit /b 1
