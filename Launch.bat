@echo off
rem Lineup launcher for Windows: finds Python 3.10 or newer, prepares a private .venv on the first run, starts lineup.py.
rem Any arguments are passed to lineup.py (for example: Launch.bat --game "D:\Games\GTA VI" --build).
setlocal EnableExtensions
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY (
    echo.
    echo Lineup needs Python 3.10 or newer, and none was found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo then run this file again. An older Python 3 cannot be used.
    goto :failed
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a private Python environment in .venv ^(first run only^) ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the .venv folder. Check that this folder is writable and run the file again.
        goto :failed
    )
)

fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo Installing numpy and Pillow into .venv ^(needs an internet connection the first time^) ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installing the packages failed. Check your internet connection and run this file again.
        echo If it keeps failing, delete the .venv folder and try once more.
        goto :failed
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

".venv\Scripts\python.exe" lineup.py %*
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" exit /b 0
if "%CODE%"=="130" exit /b 130
echo.
echo Lineup stopped with exit code %CODE%. The message above says why.
pause
exit /b %CODE%

:failed
echo.
pause
exit /b 1
