@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo The exporter has not been set up yet.
  echo Run Setup Windows.cmd first.
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "windows_session_export.py"

