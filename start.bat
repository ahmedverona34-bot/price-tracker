@echo off
REM Double-click this to open Price Tracker.
REM ASCII filename on purpose: cmd.exe mangles non-ASCII names in some
REM codepages, which makes the shortcut fail to resolve.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "price_tracker.py"
    endlocal
    exit /b 0
)

REM No virtual environment: fall back to a system Python if there is one.
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "price_tracker.py"
    endlocal
    exit /b 0
)

where python >nul 2>&1
if %errorlevel%==0 (
    start "" python "price_tracker.py"
    endlocal
    exit /b 0
)

echo.
echo   Price Tracker: no Python interpreter was found on this machine.
echo   Open this file again after creating the .venv, or build the app once:
echo   build_exe.bat
echo.
pause
endlocal
