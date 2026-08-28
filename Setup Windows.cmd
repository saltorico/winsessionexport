@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install 64-bit Python from https://www.python.org/downloads/windows/
  echo Then run this setup again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements-windows.txt
if errorlevel 1 goto :failed

echo.
echo Setup finished. Double-click Export Session Chat.cmd to use the exporter.
pause
exit /b 0

:failed
echo.
echo Setup failed. No Session data was changed.
pause
exit /b 1

