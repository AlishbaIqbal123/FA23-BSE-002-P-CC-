Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strDir

strCmd = "pythonw.exe """ & strDir & "\desktop_hub.py"""
On Error Resume Next
objShell.Run strCmd, 0, False
If Err.Number <> 0 Then
    ' Fallback to standard python if pythonw not registered
    objShell.Run "python """ & strDir & "\desktop_hub.py""", 1, False
End If
