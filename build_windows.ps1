$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($env:OS -ne "Windows_NT") {
    throw "This build must run on Windows. Use the included GitHub Actions workflow from a Mac."
}

$BuildPython = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    python -m venv .build-venv
}

& $BuildPython -m pip install --upgrade pip
& $BuildPython -m pip install -r requirements-build-windows.txt
& $BuildPython -m unittest discover -s tests -v
& $BuildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name SessionChatExporter `
    --collect-all nacl `
    --collect-all sqlcipher3 `
    windows_session_export.py

Write-Host ""
Write-Host "Windows executable created at:"
Write-Host (Join-Path $PSScriptRoot "dist\SessionChatExporter.exe")

