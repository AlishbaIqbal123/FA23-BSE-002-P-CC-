Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strDir

strCmd = "pythonw.exe """ & strDir & "\client\app.py"""
On Error Resume Next
objShell.Run strCmd, 0, False
If Err.Number <> 0 Then
    objShell.Run "python """ & strDir & "\client\app.py""", 1, False
End If
