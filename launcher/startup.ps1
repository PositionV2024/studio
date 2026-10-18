param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = ""
)

# The small native setup window has no Python or third-party dependencies.
$ErrorActionPreference = "Stop"
$env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
Remove-Item Env:PYLAUNCHER_ALLOW_INSTALL -ErrorAction SilentlyContinue
Remove-Item Env:PYLAUNCHER_ALWAYS_INSTALL -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

function ConvertTo-StudioProcessArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows CommandLineToArgvW / CRT escaping, including spaces, embedded quotes,
    # and trailing backslashes. Arguments never pass through cmd.exe or a shell.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

$script:SetupState = [pscustomobject]@{
    Process = $null
    KillProcess = $null
    PendingFailure = ""
    OutTask = $null
    ErrTask = $null
    OutDone = $true
    ErrDone = $true
    Phase = ""
    SelectedPython = ""
    ReadyPython = ""
    LastError = ""
    Busy = $false
    Cancelling = $false
    CloseAfterCancel = $false
    CancelDeadline = [DateTime]::MinValue
    CancelFile = ""
    AllowClose = $false
    ExitCode = 1
    HandoffStarted = $false
    DetailsShown = $false
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "HoYo Project Studio"
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(570, 220)
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
try {
    $iconPath = Join-Path $ProjectRoot "resources\hoyo_project_studio.ico"
    if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
        $form.Icon = New-Object System.Drawing.Icon($iconPath)
    }
} catch { }

$heading = New-Object System.Windows.Forms.Label
$heading.Location = New-Object System.Drawing.Point(24, 21)
$heading.Size = New-Object System.Drawing.Size(520, 31)
$heading.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$heading.Text = "Opening HoYo Project Studio"
$form.Controls.Add($heading)

$status = New-Object System.Windows.Forms.Label
$status.Location = New-Object System.Drawing.Point(25, 64)
$status.Size = New-Object System.Drawing.Size(518, 47)
$status.Text = "Checking your installed Python..."
$status.AccessibleName = "Setup status"
$form.Controls.Add($status)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(25, 121)
$progress.Size = New-Object System.Drawing.Size(520, 8)
$progress.Style = "Marquee"
$progress.MarqueeAnimationSpeed = 28
$form.Controls.Add($progress)

$hint = New-Object System.Windows.Forms.Label
$hint.Location = New-Object System.Drawing.Point(25, 140)
$hint.Size = New-Object System.Drawing.Size(520, 24)
$hint.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$hint.ForeColor = [System.Drawing.Color]::FromArgb(85, 85, 85)
$hint.Text = "Studio opens when the editor is ready. Preview can finish in the background."
$form.Controls.Add($hint)

$detailsButton = New-Object System.Windows.Forms.Button
$detailsButton.Location = New-Object System.Drawing.Point(25, 175)
$detailsButton.Size = New-Object System.Drawing.Size(116, 30)
$detailsButton.Text = "Show details"
$form.Controls.Add($detailsButton)

$retryButton = New-Object System.Windows.Forms.Button
$retryButton.Location = New-Object System.Drawing.Point(337, 175)
$retryButton.Size = New-Object System.Drawing.Size(98, 30)
$retryButton.Text = "Retry"
$retryButton.Visible = $false
$form.Controls.Add($retryButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Location = New-Object System.Drawing.Point(447, 175)
$cancelButton.Size = New-Object System.Drawing.Size(98, 30)
$cancelButton.Text = "Cancel"
$form.Controls.Add($cancelButton)

$details = New-Object System.Windows.Forms.TextBox
$details.Location = New-Object System.Drawing.Point(25, 226)
$details.Size = New-Object System.Drawing.Size(520, 210)
$details.Multiline = $true
$details.ReadOnly = $true
$details.ScrollBars = "Both"
$details.WordWrap = $false
$details.Font = New-Object System.Drawing.Font("Consolas", 9)
$details.Visible = $false
$details.AccessibleName = "Setup details"
$form.Controls.Add($details)

function Add-StudioSetupDetail {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    # Bound the live view; the backend keeps complete output in its log file.
    if ($details.TextLength -gt 100000) {
        $details.Text = $details.Text.Substring($details.TextLength - 70000)
    }
    $details.AppendText($Text.TrimEnd() + [Environment]::NewLine)
}

function Set-StudioSetupFailure {
    param([string]$Message)
    $script:SetupState.Busy = $false
    $script:SetupState.ExitCode = 1
    $script:SetupState.LastError = $Message
    $heading.Text = "Studio could not finish setup"
    $status.Text = $Message
    $hint.Text = "Open details, check the connection or error, then choose Retry."
    $progress.Style = "Continuous"
    $progress.Value = 0
    $retryButton.Visible = $true
    $retryButton.Enabled = $true
    $cancelButton.Text = "Close"
    $cancelButton.Enabled = $true
    Add-StudioSetupDetail -Text ("ERROR: " + $Message)
    try {
        $log = Join-Path $ProjectRoot "startup_window.log"
        (([DateTime]::Now.ToString("s")) + " " + $Message) | Add-Content -LiteralPath $log -Encoding UTF8
        Add-StudioSetupDetail -Text ("Setup log: " + (Join-Path $ProjectRoot "setup_hoyo_studio.log"))
        Add-StudioSetupDetail -Text ("Window log: " + $log)
    } catch { }
}

function Start-StudioSetupProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$Phase)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-StudioProcessArgument -Value $_ }) -join " ")
    $process.StartInfo.WorkingDirectory = $ProjectRoot
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $process.StartInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $process.StartInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
    try {
        $null = $process.Start()
        $script:SetupState.Process = $process
        $script:SetupState.OutTask = $process.StandardOutput.ReadLineAsync()
        $script:SetupState.ErrTask = $process.StandardError.ReadLineAsync()
        $script:SetupState.OutDone = $false
        $script:SetupState.ErrDone = $false
        $script:SetupState.Phase = $Phase
        $script:SetupState.Busy = $true
    } catch {
        $process.Dispose()
        throw
    }
}

function Remove-StudioCancelFile {
    if (-not [string]::IsNullOrWhiteSpace($script:SetupState.CancelFile)) {
        Remove-Item -LiteralPath $script:SetupState.CancelFile -Force -ErrorAction SilentlyContinue
    }
    $script:SetupState.CancelFile = ""
}

function Start-StudioSetupAttempt {
    if ($script:SetupState.Busy) { return }
    Remove-StudioCancelFile
    $script:SetupState.Cancelling = $false
    $script:SetupState.CloseAfterCancel = $false
    $script:SetupState.LastError = ""
    $script:SetupState.PendingFailure = ""
    $script:SetupState.SelectedPython = ""
    $script:SetupState.ReadyPython = ""
    $script:SetupState.HandoffStarted = $false
    $script:SetupState.CancelFile = Join-Path ([IO.Path]::GetTempPath()) ("hoyo-studio-setup-" + [Guid]::NewGuid().ToString("N") + ".cancel")
    $heading.Text = "Opening HoYo Project Studio"
    $status.Text = "Checking your installed Python..."
    $hint.Text = "Studio opens when the editor is ready. Preview can finish in the background."
    $retryButton.Visible = $false
    $cancelButton.Text = "Cancel"
    $cancelButton.Enabled = $true
    $progress.Style = "Marquee"
    $progress.MarqueeAnimationSpeed = 28
    Add-StudioSetupDetail -Text "Checking the local environment. Python will not be downloaded."
    try {
        $script:ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
        $discovery = Join-Path $ProjectRoot "launcher\python_discovery.ps1"
        $helper = Join-Path $ProjectRoot "launcher\startup.py"
        if (-not (Test-Path -LiteralPath $discovery -PathType Leaf) -or
            -not (Test-Path -LiteralPath $helper -PathType Leaf)) {
            throw "Setup files are missing. Extract the complete application or update, then retry."
        }
        $powerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $discovery,
            "-ResolveRuntime", "-ProjectRoot", $ProjectRoot, "-CancelFile", $script:SetupState.CancelFile)
        if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
            $arguments += @("-PythonExecutable", $PythonExecutable)
        }
        Start-StudioSetupProcess -Executable $powerShell -Arguments $arguments -Phase "discovery"
    } catch {
        Set-StudioSetupFailure -Message $_.Exception.Message
    }
}

function Read-StudioSetupLine {
    param([string]$Line, [bool]$StandardError = $false)
    if ([string]::IsNullOrWhiteSpace($Line)) { return }
    if ($StandardError) {
        Add-StudioSetupDetail -Text $Line
        return
    }
    try { $event = $Line | ConvertFrom-Json } catch {
        Add-StudioSetupDetail -Text $Line
        return
    }
    if ($event.message) { Add-StudioSetupDetail -Text ([string]$event.message) }
    switch ([string]$event.event) {
        "python" {
            if ($script:SetupState.Phase -eq "discovery") {
                $script:SetupState.SelectedPython = [string]$event.executable
                Add-StudioSetupDetail -Text ("Installed Python: " + $script:SetupState.SelectedPython)
            }
        }
        "status" {
            if (-not $script:SetupState.Cancelling -and $event.message) { $status.Text = [string]$event.message }
        }
        "ready" {
            if ($script:SetupState.Phase -eq "core" -and $event.phase -eq "core") {
                $script:SetupState.ReadyPython = [string]$event.executable
            }
        }
        "error" { $script:SetupState.LastError = [string]$event.message }
    }
}

function Stop-OwnedStudioProcess {
    # This process and its descendants were created by this window. Never kill
    # other Python installations, editors, or user terminal processes.
    $process = $script:SetupState.Process
    if ($null -eq $process -or $null -ne $script:SetupState.KillProcess) { return }
    try {
        if ($process.HasExited) { return }
        $taskKill = Join-Path $env:WINDIR "System32\taskkill.exe"
        $killer = New-Object System.Diagnostics.Process
        $killer.StartInfo.FileName = $taskKill
        $killer.StartInfo.Arguments = "/PID " + $process.Id + " /T /F"
        $killer.StartInfo.UseShellExecute = $false
        $killer.StartInfo.CreateNoWindow = $true
        try {
            $null = $killer.Start()
            $script:SetupState.KillProcess = $killer
        } catch {
            $killer.Dispose()
            throw
        }
    } catch {
        try { $process.Kill() } catch { }
    }
}

function Request-StudioSetupCancellation {
    param([bool]$CloseAfter = $true)
    if (-not $script:SetupState.Busy) {
        $script:SetupState.AllowClose = $true
        $form.Close()
        return
    }
    if ($CloseAfter) { $script:SetupState.CloseAfterCancel = $true }
    if ($script:SetupState.Cancelling) { return }
    $script:SetupState.Cancelling = $true
    $script:SetupState.CancelDeadline = [DateTime]::UtcNow.AddSeconds(8)
    $status.Text = "Cancelling setup and stopping this download..."
    $hint.Text = "Please wait while the installer closes safely."
    $cancelButton.Enabled = $false
    try {
        [IO.File]::WriteAllText($script:SetupState.CancelFile, "cancel")
    } catch {
        $script:SetupState.CancelDeadline = [DateTime]::UtcNow
    }
}

function Open-StudioAfterCoreSetup {
    $python = $script:SetupState.ReadyPython
    $expected = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    # The core helper verifies packages; only its successful, project-local
    # environment is allowed to take over the setup window.
    if ([string]::IsNullOrWhiteSpace($python) -or
        -not [string]::Equals([IO.Path]::GetFullPath($python), [IO.Path]::GetFullPath($expected), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Setup did not return a verified application environment. Open details and retry."
    }
    $pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
    $launcher = Join-Path $ProjectRoot "launcher\launcher.pyw"
    if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or
        -not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "The application launcher is missing from the prepared environment. Open details and retry."
    }
    # A shortcut is convenient but must never prevent a ready editor from opening.
    try {
        & (Join-Path $ProjectRoot "launcher\create_desktop_shortcut.ps1") `
            -ProjectRoot $ProjectRoot -UpdateManagedOnly | Out-Null
    } catch { Add-StudioSetupDetail -Text ("Desktop shortcut could not be created: " + $_.Exception.Message) }
    $status.Text = "Opening the editor..."
    $app = New-Object System.Diagnostics.Process
    $app.StartInfo.FileName = $pythonw
    $app.StartInfo.Arguments = ((@("-I", "-X", "utf8", $launcher, "--background-setup") |
        ForEach-Object { ConvertTo-StudioProcessArgument -Value $_ }) -join " ")
    $app.StartInfo.WorkingDirectory = $ProjectRoot
    $app.StartInfo.UseShellExecute = $false
    $app.StartInfo.CreateNoWindow = $true
    try { $null = $app.Start() } finally { $app.Dispose() }
    $script:SetupState.HandoffStarted = $true
    $script:SetupState.ExitCode = 0
    $script:SetupState.AllowClose = $true
    Remove-StudioCancelFile
    $form.Close()
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 100
$timer.Add_Tick({
    try {
        $state = $script:SetupState
        $process = $state.Process
        if ($null -eq $process) { return }
        # Read only completed asynchronous tasks, with a bounded batch per tick.
        # Both pipes continue draining while the window handles clicks and paint.
        for ($i = 0; $i -lt 120 -and -not $state.OutDone -and $state.OutTask.IsCompleted; $i++) {
            $line = $state.OutTask.GetAwaiter().GetResult()
            if ($null -eq $line) { $state.OutDone = $true; break }
            Read-StudioSetupLine -Line $line
            $state.OutTask = $process.StandardOutput.ReadLineAsync()
        }
        for ($i = 0; $i -lt 120 -and -not $state.ErrDone -and $state.ErrTask.IsCompleted; $i++) {
            $line = $state.ErrTask.GetAwaiter().GetResult()
            if ($null -eq $line) { $state.ErrDone = $true; break }
            Read-StudioSetupLine -Line $line -StandardError $true
            $state.ErrTask = $process.StandardError.ReadLineAsync()
        }
        if ($state.Cancelling -and -not $process.HasExited -and [DateTime]::UtcNow -ge $state.CancelDeadline) {
            Stop-OwnedStudioProcess
            $state.CancelDeadline = [DateTime]::UtcNow.AddSeconds(3)
        }
        if ($null -ne $state.KillProcess) {
            if (-not $state.KillProcess.HasExited) { return }
            $state.KillProcess.Dispose()
            $state.KillProcess = $null
        }
        if (-not $process.HasExited -or -not $state.OutDone -or -not $state.ErrDone) { return }
        $processExit = $process.ExitCode
        $phase = $state.Phase
        $process.Dispose()
        $state.Process = $null
        $state.Busy = $false
        if ($state.Cancelling) {
            Remove-StudioCancelFile
            if ($state.CloseAfterCancel) {
                $state.ExitCode = 130
                $state.AllowClose = $true
                $form.Close()
            } else {
                $state.Cancelling = $false
                Set-StudioSetupFailure -Message $state.PendingFailure
            }
            return
        }
        if ($processExit -ne 0) {
            $message = $state.LastError
            if ([string]::IsNullOrWhiteSpace($message)) { $message = "Setup stopped with exit code $processExit. Open details and retry." }
            Set-StudioSetupFailure -Message $message
            return
        }
        if ($phase -eq "discovery") {
            if ([string]::IsNullOrWhiteSpace($state.SelectedPython)) {
                throw "No verified installed Python was returned. Open details and retry."
            }
            $status.Text = "Preparing the editor and code completion..."
            $arguments = @("-I", "-X", "utf8", (Join-Path $ProjectRoot "launcher\startup.py"),
                "--project-root", $ProjectRoot, "--phase", "core", "--cancel-file", $state.CancelFile)
            Start-StudioSetupProcess -Executable $state.SelectedPython -Arguments $arguments -Phase "core"
        } elseif ($phase -eq "core") {
            Open-StudioAfterCoreSetup
        }
    } catch {
        # Keep the window alive until cooperative/forced subprocess-tree cleanup
        # finishes, even if a redirected stream or JSON-processing callback failed.
        if ($null -ne $script:SetupState.Process) {
            $script:SetupState.PendingFailure = $_.Exception.Message
            $script:SetupState.OutDone = $true
            $script:SetupState.ErrDone = $true
            Request-StudioSetupCancellation -CloseAfter $false
        } else {
            Set-StudioSetupFailure -Message $_.Exception.Message
        }
    }
})

$detailsButton.Add_Click({
    $script:SetupState.DetailsShown = -not $script:SetupState.DetailsShown
    $details.Visible = $script:SetupState.DetailsShown
    if ($script:SetupState.DetailsShown) {
        $detailsButton.Text = "Hide details"
        $form.ClientSize = New-Object System.Drawing.Size(570, 458)
    } else {
        $detailsButton.Text = "Show details"
        $form.ClientSize = New-Object System.Drawing.Size(570, 220)
    }
})
$retryButton.Add_Click({ Start-StudioSetupAttempt })
$cancelButton.Add_Click({ Request-StudioSetupCancellation })
$form.Add_FormClosing({
    param($sender, $eventArgs)
    if ($script:SetupState.Busy -and -not $script:SetupState.AllowClose) {
        $eventArgs.Cancel = $true
        Request-StudioSetupCancellation
    }
})
$form.Add_Shown({
    $timer.Start()
    Start-StudioSetupAttempt
})
try {
    [System.Windows.Forms.Application]::Run($form)
} finally {
    $timer.Stop()
    $timer.Dispose()
    if ($null -ne $script:SetupState.Process) {
        Stop-OwnedStudioProcess
        if ($null -ne $script:SetupState.KillProcess) {
            try { $null = $script:SetupState.KillProcess.WaitForExit(10000) } catch { }
            $script:SetupState.KillProcess.Dispose()
            $script:SetupState.KillProcess = $null
        }
        try { $null = $script:SetupState.Process.WaitForExit(3000) } catch { }
        try { $script:SetupState.Process.Dispose() } catch { }
    }
    Remove-StudioCancelFile
    $form.Dispose()
}
exit $script:SetupState.ExitCode
