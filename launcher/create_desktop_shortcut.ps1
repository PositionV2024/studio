param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "HoYo Project Studio.lnk"
$launcherPath = Join-Path $ProjectRoot "Launch HoYo Project Studio.vbs"
$iconPath = Join-Path $ProjectRoot "resources\hoyo_project_studio.ico"

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:WINDIR\System32\wscript.exe"
$shortcut.Arguments = '"' + $launcherPath + '"'
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.Description = "Launch HoYo Project Studio"

if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}

$shortcut.Save()
Write-Host "Desktop shortcut created: $shortcutPath" -ForegroundColor Green
