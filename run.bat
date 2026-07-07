@echo off
setlocal
cd /d "%~dp0"

rem ---- find Python 3 ----
set "PYCMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo Python 3 was not found. Please install it from https://www.python.org/downloads/
    echo During installation, check "Add python.exe to PATH".
    pause
    exit /b 1
)

rem ---- require Python 3.9+ (needed by PySide6) ----
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Your Python is too old for this application - version 3.9 or newer is
    echo required. Please install the current version from
    echo https://www.python.org/downloads/ and run this again.
    %PYCMD% --version
    pause
    exit /b 1
)

rem ---- create venv if missing ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

rem ---- install dependencies only when requirements.txt changed ----
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies - this can take a few minutes on first run...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency installation FAILED. Check the messages above -
        echo usually the computer is offline or a proxy blocks python.org.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

rem ---- run ----
rem Default: launch the GUI app. "run.bat probe" runs the legacy read-only
rem probe (development tool, lives in dev\).
if /I "%~1"=="probe" (
    ".venv\Scripts\python.exe" "dev\probe.py" %2 %3 %4 %5
    pause
    exit /b 0
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 (
    echo.
    echo ============================================================
    echo The application ended with an error. The message above and
    echo the file error.log next to run.bat contain the details.
    echo ============================================================
    if exist error.log (
        echo --- last lines of error.log ---
        powershell -NoProfile -Command "Get-Content error.log -Tail 20"
    )
    pause
    exit /b 1
)
