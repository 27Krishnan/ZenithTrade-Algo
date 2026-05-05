Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strPath = fso.GetParentFolderName(WScript.ScriptFullName)

' Change directory to the script's folder
WshShell.CurrentDirectory = strPath

' Run the background batch file hidden (0)
' We use cmd /c to ensure the batch environment is correctly initialized
WshShell.Run "cmd /c " & chr(34) & strPath & "\run_background.bat" & chr(34), 0, False

Set WshShell = Nothing
Set fso = Nothing
