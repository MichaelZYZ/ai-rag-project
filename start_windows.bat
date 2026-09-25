@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
    if errorlevel 1 (
        echo Python 3.9 or newer is required.
        goto :error
    )
    set "PYTHON_CMD=python"
)

if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv ".venv"
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
".venv\Scripts\python.exe" seed.py
if errorlevel 1 goto :error

".venv\Scripts\python.exe" run_server.py
if errorlevel 1 goto :error
exit /b 0

:error
echo Startup failed. Review the error above.
pause
exit /b 1
