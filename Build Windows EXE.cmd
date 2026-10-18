@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto directory_failed
title Build HoYo Project Studio EXE

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\launcher\build_exe.ps1" -ProjectRoot "%CD%"
set "STUDIO_BUILD_EXIT=%errorlevel%"
if not "%STUDIO_BUILD_EXIT%"=="0" goto failed

exit /b 0

:failed
echo.
echo [ERROR] The executable build failed. See the details above.
echo Build log: "%CD%\build_windows_exe.log"
pause
exit /b %STUDIO_BUILD_EXIT%

:directory_failed
echo.
echo [ERROR] The application folder could not be opened: "%~dp0"
pause
exit /b 1
