@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Please install Python 3.11+ and follow README setup steps first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m sharemouse.windows
if errorlevel 1 pause
