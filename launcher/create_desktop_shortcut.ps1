param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$UpdateManagedOnly
)

$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "HoYo Project Studio.lnk"
$nativeLauncher = Join-Path $ProjectRoot "HoYo Project Studio.exe"
$legacyLauncher = Join-Path $ProjectRoot "Launch HoYo Project Studio.vbs"
$iconPath = Join-Path $ProjectRoot "resources\hoyo_project_studio.ico"

# In the compact download, ProjectRoot is the app folder. The only public
# entry point lives one level above it. Flat source checkouts remain supported.
if ((Split-Path -Leaf $ProjectRoot) -ieq "app") {
    $outerLauncher = Join-Path (Split-Path -Parent $ProjectRoot) "HoYo Project Studio.exe"
    if (Test-Path -LiteralPath $outerLauncher -PathType Leaf) {
        $nativeLauncher = $outerLauncher
    }
}

$wshShell = New-Object -ComObject WScript.Shell
$shortcut = $wshShell.CreateShortcut($shortcutPath)
if ($UpdateManagedOnly -and (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
    $targetName = [IO.Path]::GetFileName($shortcut.TargetPath)
    $isNative = ($targetName -ieq "HoYo Project Studio.exe") -and
        [string]::IsNullOrWhiteSpace($shortcut.Arguments)
    $isLegacy = ($targetName -ieq "wscript.exe") -and
        ($shortcut.Arguments -match '^"[^"\r\n]+\\Launch HoYo Project Studio\.vbs"$')
    if (($shortcut.Description -ne "Launch HoYo Project Studio") -or
        -not ($isNative -or $isLegacy)) {
        Write-Host "An existing custom desktop shortcut was kept."
        return
    }
}
if (Test-Path -LiteralPath $nativeLauncher -PathType Leaf) {
    $shortcut.TargetPath = $nativeLauncher
    $shortcut.Arguments = ""
} elseif (Test-Path -LiteralPath $legacyLauncher -PathType Leaf) {
    # Existing installations can still create a shortcut before applying the EXE.
    $shortcut.TargetPath = [IO.Path]::Combine([Environment]::SystemDirectory, "wscript.exe")
    $shortcut.Arguments = '"' + $legacyLauncher + '"'
} else {
    throw "The application launcher is missing. Extract the complete application into this folder, then try again."
}
$shortcut.WorkingDirectory = Split-Path -Parent $nativeLauncher
if (-not (Test-Path -LiteralPath $nativeLauncher -PathType Leaf)) {
    $shortcut.WorkingDirectory = $ProjectRoot
}
$shortcut.Description = "Launch HoYo Project Studio"

if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
    $shortcut.IconLocation = "$iconPath,0"
} elseif (Test-Path -LiteralPath $nativeLauncher -PathType Leaf) {
    $shortcut.IconLocation = "$nativeLauncher,0"
}

$shortcut.Save()
Write-Host "Desktop shortcut created: $shortcutPath" -ForegroundColor Green
