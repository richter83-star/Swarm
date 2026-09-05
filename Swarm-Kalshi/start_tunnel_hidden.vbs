Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

REM Launch resilient VPS dashboard tunnel in silent background mode (0 = hide window)
WshShell.Run "pythonw scripts\tunnel_vps_dashboard.py", 0, False
