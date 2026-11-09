' Launch a Caliber shadow job with no console window.
' Task Scheduler calls: wscript.exe //B //Nologo shadow_schedule_hidden.vbs <job>
If WScript.Arguments.Count < 1 Then WScript.Quit 1

Dim job
job = WScript.Arguments(0)
If Not RegExpMatch(job, "^[a-z0-9-]+$") Then WScript.Quit 1

Dim shell, folder, command
Set shell = CreateObject("Wscript.Shell")
shell.Environment("PROCESS")("CALIBER_SCHEDULED") = "1"
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & folder & "\shadow_schedule.ps1"" -Job " & job
shell.Run command, 0, True

Function RegExpMatch(value, pattern)
  Dim re
  Set re = New RegExp
  re.Pattern = pattern
  RegExpMatch = re.Test(value)
End Function
