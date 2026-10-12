@echo off
setlocal
cd /d "%~dp0"

echo ======================================================================
echo   Starting Distributed Computing Control Hub...
echo ======================================================================

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.11+ from https://www.python.org/
    pause
    exit /b 1
)

python -c "import PyQt6" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [INFO] Installing required dependencies...
    pip install -r requirements.txt
)

start "" pythonw.exe desktop_hub.py
if %ERRORLEVEL% neq 0 (
    python desktop_hub.py
)
