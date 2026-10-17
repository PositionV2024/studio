Option Explicit

Dim shell, fso, root, pythonw, launcher, setupFile, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\.venv\Scripts\pythonw.exe"
launcher = root & "\launcher\launcher.pyw"
setupFile = root & "\Setup HoYo Project Studio.cmd"

If Not fso.FileExists(pythonw) Then
    shell.Run Chr(34) & setupFile & Chr(34), 1, True
End If

If fso.FileExists(pythonw) Then
    shell.CurrentDirectory = root
    command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & launcher & Chr(34)
    shell.Run command, 0, False
Else
    MsgBox "HoYo Project Studio could not find its Python environment. Run Setup HoYo Project Studio.cmd.", 16, "HoYo Project Studio"
End If
