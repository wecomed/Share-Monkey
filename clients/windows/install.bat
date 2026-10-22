@echo off
setlocal
cd /d "%~dp0"
if not exist requirements.txt cd /d "%~dp0..\.."
py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1
if errorlevel 1 (
  echo Source install requires Python 3.11+. The EXE setup includes Python automatically.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Installation complete. Open run-windows.bat to start the Windows client.
pause
exit /b 0
:failed
echo Installation failed. Copy the error message for troubleshooting.
pause
exit /b 1
