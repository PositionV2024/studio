Option Explicit

Dim shell, fso, root, nativeLauncher, outerLauncher, startup, powershell, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
nativeLauncher = root & "\HoYo Project Studio.exe"
If LCase(fso.GetFileName(root)) = "app" Then
    outerLauncher = fso.GetParentFolderName(root) & "\HoYo Project Studio.exe"
    If fso.FileExists(outerLauncher) Then nativeLauncher = outerLauncher
End If
startup = root & "\launcher\startup.ps1"
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"

If fso.FileExists(nativeLauncher) Then
    command = Chr(34) & nativeLauncher & Chr(34)
Else
    If Not fso.FileExists(startup) Then
        MsgBox "The setup launcher is missing. Extract the complete application or update into this folder, then try again.", 16, "HoYo Project Studio"
        WScript.Quit 1
    End If
    command = Chr(34) & powershell & Chr(34) & " -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & startup & Chr(34)
End If

shell.CurrentDirectory = root
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "HoYo Project Studio could not open its setup window: " & Err.Description, 16, "HoYo Project Studio"
    WScript.Quit 1
End If
