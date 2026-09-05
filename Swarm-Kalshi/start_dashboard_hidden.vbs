Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

REM Launch dashboard in silent background mode (0 = hide window)
WshShell.Run "python manage_swarm.py dashboard -d", 0, False
