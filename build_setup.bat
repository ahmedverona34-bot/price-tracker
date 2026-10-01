@echo off
REM Build the installable setup: dist-installer\PriceTracker-Setup-<ver>.exe
REM (run from the project folder)
REM
REM Requires: .venv with pyinstaller, and Inno Setup 6 (ISCC.exe).
REM Install Inno Setup with:  winget install JRSoftware.InnoSetup
REM
REM Step 1 builds the app with build_exe.bat; step 2 compiles installer.iss
REM around it, producing one file the user downloads and double-clicks.
REM
REM The user picks the install folder (default: %LOCALAPPDATA%\PriceTracker).
REM No administrator rights are ever requested: the app keeps its data in
REM %APPDATA%\PriceTracker, so a later in-app update needs no UAC prompt.
setlocal

if not exist "dist\PriceTracker\PriceTracker.exe" (
    echo No build found, running build_exe.bat first...
    call build_exe.bat
    if errorlevel 1 exit /b 1
)

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
