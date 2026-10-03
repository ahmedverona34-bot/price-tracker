@echo off
REM Build the installable setup: dist-installer\PriceTracker-Setup-<ver>.exe
REM (run from the project folder)
REM
REM Requires: a venv with pyinstaller (build_exe.bat finds it), and Inno
REM Setup 6 (ISCC.exe). Install Inno Setup with:
REM     winget install JRSoftware.InnoSetup
REM
REM Step 1 always rebuilds the app with build_exe.bat; step 2 compiles
REM installer.iss around it, producing one file the user downloads and
REM double-clicks.
REM
REM The user picks the install folder (default: %LOCALAPPDATA%\PriceTracker).
REM No administrator rights are ever requested: the app keeps its data in
REM %APPDATA%\PriceTracker, so a later in-app update needs no UAC prompt.
setlocal

REM Always rebuild the app first, never reuse whatever is in dist/.
REM This used to be skipped whenever dist\PriceTracker\PriceTracker.exe
REM already existed, which silently packaged a stale binary: the published
REM 1.2.10 was built from a dist/ left over from hours earlier, before the
REM commits it was named for had even landed. A setup is only trustworthy if
REM the app inside it was built from the source tree as it stands right now.
call build_exe.bat
if errorlevel 1 exit /b 1

REM Find ISCC.exe: a normal install, then the per-user one.
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not defined ISCC if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not defined ISCC (
    echo ERROR: Inno Setup 6 not found.
    echo Install it with:  winget install JRSoftware.InnoSetup
    exit /b 1
)

echo Using %ISCC%
"%ISCC%" installer\installer.iss
if errorlevel 1 exit /b 1

echo.
echo SETUP DONE: dist-installer\PriceTracker-Setup-*.exe
endlocal
