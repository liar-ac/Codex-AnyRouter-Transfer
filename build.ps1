$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv\Scripts\python.exe")) {
  py -3.12 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Try to locate UPX so PyInstaller can compress the bundled binaries.
# UPX is optional — without it the build still works, just larger.
function Find-UpxDir {
  # 1) UPX on PATH
  $cmd = Get-Command upx.exe -ErrorAction SilentlyContinue
  if ($cmd) { return (Split-Path $cmd.Path -Parent) }

  # 2) UPX_DIR env var (manual override)
  if ($env:UPX_DIR -and (Test-Path (Join-Path $env:UPX_DIR 'upx.exe'))) {
    return $env:UPX_DIR
  }

  # 3) winget-installed UPX
  $wingetRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
  if (Test-Path $wingetRoot) {
    $hit = Get-ChildItem $wingetRoot -Filter 'UPX.UPX_*' -Directory -ErrorAction SilentlyContinue |
      ForEach-Object { Get-ChildItem $_.FullName -Filter 'upx-*-win64' -Directory -ErrorAction SilentlyContinue } |
      Select-Object -First 1
    if ($hit -and (Test-Path (Join-Path $hit.FullName 'upx.exe'))) {
      return $hit.FullName
    }
  }
  return $null
}

$upxDir = Find-UpxDir
$upxArgs = @()
if ($upxDir) {
  Write-Host "UPX detected at: $upxDir (will compress binaries)"
  $upxArgs = @('--upx-dir', $upxDir)
} else {
  Write-Host "UPX not found — building without binary compression."
  Write-Host "  Install with: winget install UPX.UPX  (then re-run this script)"
}

# Use the maintained .spec file (it pins hiddenimports=['wsproto', ...] which
# the WebSocket deny route in app.py needs, plus the excludes/optimize pass
# that keeps the exe lean). Driving PyInstaller via the spec also keeps
# --windowed / --onefile consistent across rebuilds.
.\.venv\Scripts\python.exe -m PyInstaller `
  --noconfirm `
  --workpath build-venv `
  @upxArgs `
  CodexAnyRoute.spec

Write-Host ""
Write-Host "Build complete: dist\CodexAnyRoute.exe"
$exe = Get-Item dist\CodexAnyRoute.exe -ErrorAction SilentlyContinue
if ($exe) {
  $sizeMB = [math]::Round($exe.Length / 1MB, 2)
  Write-Host ("  Size: {0} MB" -f $sizeMB)
}
