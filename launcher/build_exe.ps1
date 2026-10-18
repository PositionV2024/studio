param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = ""
)

$ErrorActionPreference = "Stop"
$helperStarted = $false
try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
    Set-Location -LiteralPath $ProjectRoot
    $helper = Join-Path $ProjectRoot "launcher\windows_build.py"
    $discovery = Join-Path $ProjectRoot "launcher\python_discovery.ps1"
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf) -or
        -not (Test-Path -LiteralPath $discovery -PathType Leaf)) {
        throw "The Windows build helper is missing. Extract the complete update into the application folder."
    }

    . $discovery -ProjectRoot $ProjectRoot -PythonExecutable $PythonExecutable
    $selected = Find-StudioPython -ProjectRoot $ProjectRoot -PythonExecutable $PythonExecutable
    Write-Host "Using installed Python: $($selected.executable)" -ForegroundColor Cyan
    $helperStarted = $true
    & $selected.executable -I -X utf8 $helper --project-root $ProjectRoot
    $buildExit = $LASTEXITCODE
    exit $buildExit
} catch {
    $message = "[ERROR] Windows executable build failed: $($_.Exception.Message)"
    Write-Host $message -ForegroundColor Red
    if (-not $helperStarted) {
        try {
            $log = Join-Path $ProjectRoot "build_windows_exe.log"
            $message | Set-Content -LiteralPath $log -Encoding UTF8
            Write-Host "Build log: $log"
        } catch { }
    }
    exit 1
}
