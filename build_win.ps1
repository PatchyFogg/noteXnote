# Build noteXnote.exe on Windows via PyInstaller.
#
# Not verified against a real Windows run from this session (built on
# macOS, no Windows environment available here) — run this in the actual
# Win10 VM per the project's existing cross-platform workflow, and expect
# to iterate on it. Needs a real python.org Python 3.10+ on PATH (not the
# Microsoft Store stub) and ffmpeg reachable separately at runtime — this
# script only builds the app itself, it doesn't install ffmpeg/yt-dlp/spotdl.

$ErrorActionPreference = "Stop"

Write-Host "=== Building noteXnote (Windows) ==="

if (-not (Test-Path .venv)) {
    Write-Host "Creating venv..."
    python -m venv .venv
}
. .venv\Scripts\Activate.ps1

Write-Host "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

Write-Host "Building .exe..."
pyinstaller --noconfirm --windowed --onefile `
    --name noteXnote `
    --icon noteXnote.ico `
    --add-data "HELP.html;." `
    --add-data "progress.gif;." `
    --collect-all sounddevice `
    notexnote.py

Write-Host ""
Write-Host "=== Done ==="
Write-Host "  EXE: dist\noteXnote.exe"
Write-Host ""
Write-Host "Sanity-check the core audio pipeline without launching the GUI:"
Write-Host "  .venv\Scripts\python notexnote.py --selftest"
