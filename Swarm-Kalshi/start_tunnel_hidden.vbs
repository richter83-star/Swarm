Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

REM Launch resilient 24/7 VPS dashboard tunnel watchdog in silent background mode (0 = hide window)
targetScript = scriptDir & "\scripts\tunnel_vps_dashboard.py"
WshShell.Run "pythonw """ & targetScript & """", 0, False
