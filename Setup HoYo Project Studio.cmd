@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 1

rem The compact download keeps its public EXE outside this app folder.
for %%I in ("%CD%") do set "studio_folder_name=%%~nxI"
if /i "%studio_folder_name%"=="app" if exist "%CD%\..\HoYo Project Studio.exe" (
    start "HoYo Project Studio" "%CD%\..\HoYo Project Studio.exe"
    if errorlevel 1 exit /b 1
    exit /b 0
)

rem The included EXE opens the same resumable setup window without a console.
if exist "%CD%\HoYo Project Studio.exe" (
    start "HoYo Project Studio" "%CD%\HoYo Project Studio.exe"
    if errorlevel 1 exit /b 1
    exit /b 0
)

rem Compatibility for older application folders without the new launcher.
start "HoYo Project Studio" "%SystemRoot%\System32\wscript.exe" "%CD%\Launch HoYo Project Studio.vbs"
if errorlevel 1 exit /b 1
exit /b 0
