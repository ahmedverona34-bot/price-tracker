' Double-click this to open Price Tracker with no console window at all.
' Preferred over start.bat for a desktop shortcut: a batch file flashes a
' black console window on every launch, this does not.
'
' Only the .venv interpreter is used. If it is missing we say so instead of
' silently falling back, so a broken shortcut is never mistaken for a working
' one.
Option Explicit

Dim fso, shell, here, venvPy
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here   = fso.GetParentFolderName(WScript.ScriptFullName)
venvPy = here & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(venvPy) Then
    MsgBox "Price Tracker: the virtual environment is missing." & vbCrLf & _
           vbCrLf & "Expected here:" & vbCrLf & venvPy & vbCrLf & vbCrLf & _
           "Create it, or build the app once with build_exe.bat.", _
           vbExclamation, "Price Tracker"
    WScript.Quit 1
End If

shell.CurrentDirectory = here
' 0 = hidden window, False = do not wait for the app to exit.
shell.Run """" & venvPy & """ """ & here & "\price_tracker.py""", 0, False
