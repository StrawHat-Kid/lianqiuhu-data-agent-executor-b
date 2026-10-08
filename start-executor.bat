@echo off
setlocal DisableDelayedExpansion

cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: Cannot switch to the executor directory.
    exit /b 1
)

set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating virtual environment...
    set "BOOTSTRAP_PY="

    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
    if not errorlevel 1 set "BOOTSTRAP_PY=py -3"

    if not defined BOOTSTRAP_PY (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
        if not errorlevel 1 set "BOOTSTRAP_PY=python"
    )

    if not defined BOOTSTRAP_PY (
        echo ERROR: Python 3.9 or later was not found.
        exit /b 1
    )

    %BOOTSTRAP_PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: Virtual environment creation failed.
        exit /b 1
    )
)

if not exist "%VENV_PY%" (
    echo ERROR: Virtual environment Python was not created.
    exit /b 1
)

echo Installing required packages...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    exit /b 1
)

echo Starting HC data-agent executor on the configured host and port...
"%VENV_PY%" main.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" echo ERROR: Executor exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
