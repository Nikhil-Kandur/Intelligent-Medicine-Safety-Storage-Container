@echo off
setlocal
cd /d "%~dp0server"
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating private Python environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if errorlevel 1 (
    echo Could not create the Python environment. Install Python 3.10+ from https://www.python.org/downloads/ and enable Add Python to PATH.
    pause
    exit /b 1
  )
)
echo [2/3] Installing required packages (Flask and pyserial)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Package installation failed. Check your internet connection and try again.
  pause
  exit /b 1
)
echo [3/3] Starting the local MedBox website and ESP32 USB reader...
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
endlocal
