param(
    [switch]$ResolveRuntime,
    [string]$ProjectRoot = "",
    [string]$PythonExecutable = "",
    [string]$CancelFile = ""
)

# Shared installed-runtime discovery for setup and Windows EXE builds.
# Dot-source this file for Find-StudioPython, or use -ResolveRuntime for JSONL.
$script:StudioDiscoveryCancelFile = ""

function Test-StudioDiscoveryCancelled {
    return (-not [string]::IsNullOrWhiteSpace($script:StudioDiscoveryCancelFile) -and
        (Test-Path -LiteralPath $script:StudioDiscoveryCancelFile -PathType Leaf))
}

# Discovery must never ask either Windows Python launcher to install a runtime.
# The legacy flags are enabled by their presence, even when their value is "0".
$env:PYTHON_MANAGER_AUTOMATIC_INSTALL = "false"
Remove-Item Env:PYLAUNCHER_ALLOW_INSTALL -ErrorAction SilentlyContinue
Remove-Item Env:PYLAUNCHER_ALWAYS_INSTALL -ErrorAction SilentlyContinue

function Invoke-DiscoveryProcess {
    param([string]$FileName, [string]$Arguments)

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $FileName
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $process.StartInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not $process.WaitForExit(200)) {
            if ((Test-StudioDiscoveryCancelled) -or [DateTime]::UtcNow -ge $deadline) {
                try { $process.Kill() } catch { }
                return $null
            }
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Output = $stdout.GetAwaiter().GetResult()
            ErrorOutput = $stderr.GetAwaiter().GetResult()
        }
    } catch {
        return $null
    } finally {
        $process.Dispose()
    }
}

function Get-CompatiblePython {
    param([string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    # Skip Microsoft Store app-execution aliases; opening these can show a Store tab.
    if ($Candidate -match '(?i)\\Microsoft\\WindowsApps\\(?:python(?:3(?:\.\d+)?)?\.exe)$') {
        return $null
    }
    if (Test-StudioDiscoveryCancelled) { return $null }
    $probe = '-I -X utf8 -c "import json,sys,struct; print(json.dumps({''executable'':sys.executable,''major'':sys.version_info[0],''minor'':sys.version_info[1],''bits'':struct.calcsize(''P'')*8,''implementation'':sys.implementation.name}))"'
    $result = Invoke-DiscoveryProcess -FileName $Candidate -Arguments $probe
    if ($null -eq $result -or $result.ExitCode -ne 0) { return $null }
    try {
        $details = $result.Output.Trim() | ConvertFrom-Json
        if (
            $details.implementation -eq "cpython" -and
            $details.major -eq 3 -and $details.minor -ge 11 -and
            $details.bits -eq 64 -and
            (Test-Path -LiteralPath $details.executable -PathType Leaf)
        ) {
            return $details
        }
    } catch { }
    return $null
}

function Add-PythonCandidate {
    param([string]$Candidate)

    if ((Test-StudioDiscoveryCancelled) -or [string]::IsNullOrWhiteSpace($Candidate)) { return }
    try {
        $candidatePath = [Environment]::ExpandEnvironmentVariables($Candidate.Trim().Trim('"'))
        $candidatePath = [IO.Path]::GetFullPath($candidatePath)
        if ((Test-Path -LiteralPath $candidatePath -PathType Leaf) -and $seenCandidates.Add($candidatePath)) {
            $candidates.Add($candidatePath)
        }
    } catch { }
}

function Find-StudioPython {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$PythonExecutable = "",
        [string]$CancelFile = ""
    )
    $script:StudioDiscoveryCancelFile = $CancelFile
    # A working project environment needs no launcher search or new environment.
    $selected = Get-CompatiblePython -Candidate (Join-Path $ProjectRoot ".venv\Scripts\python.exe")
    if ($null -eq $selected -and -not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $selected = Get-CompatiblePython -Candidate $PythonExecutable
        if ($null -eq $selected) {
            throw "The supplied Python executable is not a working 64-bit CPython 3.11 or newer: $PythonExecutable"
        }
    }

    if (Test-StudioDiscoveryCancelled) { throw "Python discovery was cancelled." }

    if ($null -eq $selected) {
        $candidates = New-Object 'System.Collections.Generic.List[string]'
        $seenCandidates = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

        # List installed runtimes, then execute their actual paths. Do not invoke a
        # version selector such as 'py -3.12', which may trigger an installation.
        foreach ($launcher in @(Get-Command py.exe,pymanager.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $listing = Invoke-DiscoveryProcess -FileName $launcher.Source -Arguments "--list-paths"
            if ($null -eq $listing -or $listing.ExitCode -ne 0) { continue }
            foreach ($line in (($listing.Output + "`n" + $listing.ErrorOutput) -split "`r?`n")) {
                if ($line -match '((?:[A-Za-z]:\\|\\\\)[^\r\n]*?python(?:3(?:\.\d+)?)?\.exe)"?\s*\*?\s*$') {
                    Add-PythonCandidate -Candidate $Matches[1]
                }
            }
        }
        foreach ($command in @(Get-Command python.exe,python3.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
            Add-PythonCandidate -Candidate $command.Source
        }

        # Also find traditional installations that were not added to PATH.
        foreach ($registryRoot in @(
            "HKCU:\Software\Python\PythonCore",
            "HKLM:\Software\Python\PythonCore",
            "HKLM:\Software\WOW6432Node\Python\PythonCore"
        )) {
            foreach ($versionKey in @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
                $installKey = Get-Item -LiteralPath (Join-Path $versionKey.PSPath "InstallPath") -ErrorAction SilentlyContinue
                if ($null -eq $installKey) { continue }
                Add-PythonCandidate -Candidate $installKey.GetValue("ExecutablePath")
                $installRoot = $installKey.GetValue("")
                if (-not [string]::IsNullOrWhiteSpace($installRoot)) {
                    Add-PythonCandidate -Candidate (Join-Path $installRoot "python.exe")
                }
            }
        }
        $installPatterns = @()
        if ($env:LOCALAPPDATA) {
            $installPatterns += Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"
            $installPatterns += Join-Path $env:LOCALAPPDATA "Python\pythoncore-*\python.exe"
        }
        if ($env:ProgramFiles) {
            $installPatterns += Join-Path $env:ProgramFiles "Python*\python.exe"
        }
        foreach ($pattern in $installPatterns) {
            foreach ($installed in @(Get-Item -Path $pattern -ErrorAction SilentlyContinue)) {
                Add-PythonCandidate -Candidate $installed.FullName
            }
        }
        foreach ($candidate in $candidates) {
            if (Test-StudioDiscoveryCancelled) { break }
            $selected = Get-CompatiblePython -Candidate $candidate
            if ($null -ne $selected) { break }
        }
    }

    if ($null -eq $selected) {
        if (Test-StudioDiscoveryCancelled) { throw "Python discovery was cancelled." }
        throw "No working installed 64-bit CPython 3.11 or newer was found. No Python was downloaded. Make your existing installation available through PATH or the Python launcher, then retry. You can also pass its full path using -PythonExecutable in launcher\startup.ps1 or launcher\build_exe.ps1."
    }

    return $selected
}

if ($ResolveRuntime) {
    $ErrorActionPreference = "Stop"
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    try {
        $selected = Find-StudioPython -ProjectRoot $ProjectRoot -PythonExecutable $PythonExecutable -CancelFile $CancelFile
        @{ event = "python"; message = "Using installed Python."; executable = $selected.executable } |
            ConvertTo-Json -Compress | Write-Output
        exit 0
    } catch {
        @{ event = "error"; message = $_.Exception.Message } | ConvertTo-Json -Compress | Write-Output
        if (Test-StudioDiscoveryCancelled) { exit 130 }
        exit 1
    }
}
