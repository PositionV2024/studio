param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "The project virtual environment was not found. Run 'Setup HoYo Project Studio.cmd' first."
}

Write-Host "Installing/updating PyInstaller..." -ForegroundColor Cyan
& $python -m pip install --upgrade "pyinstaller>=6.10,<7"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Building the Windows application..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean "HoYoProjectStudio.spec"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$exe = Join-Path $ProjectRoot "dist\HoYo Project Studio\HoYo Project Studio.exe"
Write-Host "" 
Write-Host "Build complete:" -ForegroundColor Green
Write-Host $exe -ForegroundColor White

if (Test-Path $exe) {
    Start-Process explorer.exe -ArgumentList "/select,`"$exe`""
}
