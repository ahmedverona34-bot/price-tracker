@echo off
REM Build the PriceTracker Windows app (run from the project folder).
REM Requires: .venv with pyinstaller (pip install pyinstaller)
REM
REM Produces: dist\PriceTracker\PriceTracker.exe  (a folder build, ~25 MB)
REM
REM What ships and what does not:
REM   Ships:  ui/ (interface + 5 Thmanyah Sans OTFs) and sites.json, both under
REM           dist\PriceTracker\_internal\
REM   No:     Playwright. It bundles a private copy of node (~89 MB) and no
REM           configured site needs it, so it is excluded; that is where the
REM           build went from 128 MB to 25 MB. A site asking for
REM           "use_playwright" reports a plain Arabic notice instead of crashing.
REM   No:     WebView2. Windows 10/11 already ships it; the app shows a
REM           download page if it is ever missing.
REM
REM Data (settings, app.log, prices.xlsx) is written to %APPDATA%\PriceTracker,
REM never next to the exe, so the app runs read-only from Program Files.
REM
REM To make the installable setup as well, run build_setup.bat.
setlocal

REM Look for pyinstaller rather than assuming one venv name. Both .venv and
REM .venv312 have been used in this repo, and the old line hardcoded .venv,
REM so the build failed on a checkout that had the other one.
set PY=
if exist ".venv\Scripts\pyinstaller.exe" set PY=.venv\Scripts\pyinstaller.exe
if not defined PY if exist ".venv312\Scripts\pyinstaller.exe" set PY=.venv312\Scripts\pyinstaller.exe
if not defined PY if exist "venv\Scripts\pyinstaller.exe" set PY=venv\Scripts\pyinstaller.exe
if not defined PY (
    echo ERROR: pyinstaller.exe not found.
    echo Create a venv and install it:  pip install pyinstaller
    echo Looked in: .venv, .venv312, venv
    exit /b 1
)

echo Using %PY%

REM The spec is the single source of truth. Keeping the build in one file stops
REM the command line and the spec from drifting apart.
"%PY%" --noconfirm PriceTracker.spec
if errorlevel 1 exit /b 1

REM sites.json must sit beside the exe so the user can edit it to add a store.
REM PyInstaller puts datas under _internal\, so copy it up to the app root as
REM well: sites_path() checks the program folder first, then the bundle.
copy /Y sites.json "dist\PriceTracker\sites.json" >nul
if not exist "dist\PriceTracker\sites.json" (
    echo ERROR: sites.json was not staged next to the exe.
    exit /b 1
)

REM sites.default.json is the pristine copy the app merges against on launch:
REM new default stores are added, user deletions are never resurrected.
copy /Y sites.json "dist\PriceTracker\sites.default.json" >nul
if not exist "dist\PriceTracker\sites.default.json" (
    echo ERROR: sites.default.json was not staged next to the exe.
    exit /b 1
)

echo.
echo BUILD DONE: dist\PriceTracker\PriceTracker.exe
endlocal
