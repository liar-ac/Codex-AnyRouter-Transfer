$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv\Scripts\python.exe")) {
  py -3.12 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Use the maintained .spec file (it pins hiddenimports=['wsproto', ...] which
# the WebSocket deny route in app.py needs). Driving PyInstaller via the spec
# also ensures --windowed / --onefile stay consistent across rebuilds without
# us having to keep two sources of truth.
.\.venv\Scripts\python.exe -m PyInstaller `
  --noconfirm `
  --workpath build-venv `
  CodexAnyRoute.spec

Write-Host ""
Write-Host "Build complete: dist\CodexAnyRoute.exe"
