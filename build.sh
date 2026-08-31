#!/bin/bash
set -euo pipefail

APP_NAME="noteXnote"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYFW="/Library/Frameworks/Python.framework/Versions/3.13"
CONDA="/usr/local/Caskroom/miniconda/base/lib"

echo "=== Building $APP_NAME ==="

# Venv
if [ ! -d ".venv" ]; then
    echo "Creating venv..."
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt py2app

# Clean
rm -rf build dist

echo "Building .app..."
python setup.py py2app 2>&1 | tail -5

BUNDLE="dist/${APP_NAME}.app"
if [ ! -d "$BUNDLE" ]; then
    echo "ERROR: Build failed"
    exit 1
fi

# ── Fix missing dylibs ──────────────────────────────────────────────
echo "Fixing dylib linkage..."

FW="$BUNDLE/Contents/Frameworks"
mkdir -p "$FW"

copy_lib() {
    local name="$1" src="$2"
    if [ -f "$src" ]; then
        cp "$src" "$FW/$name"
        chmod 755 "$FW/$name"
    else
        echo "  WARNING: $name not found at $src"
    fi
}

copy_lib libffi.8.dylib       "$CONDA/libffi.8.dylib"
copy_lib libbz2.dylib         "$CONDA/libbz2.dylib"
copy_lib libexpat.1.dylib     "$CONDA/libexpat.1.dylib"
copy_lib liblzma.5.dylib      "$CONDA/liblzma.5.dylib"
copy_lib libmpdec.4.dylib     "$CONDA/libmpdec.4.dylib"
copy_lib libsqlite3.0.dylib   "$CONDA/libsqlite3.0.dylib"
copy_lib libz.1.dylib         "$CONDA/libz.1.dylib"
copy_lib libcrypto.3.dylib    "$PYFW/lib/libcrypto.3.dylib"
copy_lib libssl.3.dylib       "$PYFW/lib/libssl.3.dylib"
copy_lib libtcl8.6.dylib      "$PYFW/Frameworks/Tcl.framework/Versions/8.6/Tcl"
copy_lib libtk8.6.dylib       "$PYFW/Frameworks/Tk.framework/Versions/8.6/Tk"

# Bundle PortAudio where sounddevice expects it
echo "Bundling PortAudio..."
PA_SRC=".venv/lib/python3.13/site-packages/_sounddevice_data/portaudio-binaries/libportaudio.dylib"
if [ -f "$PA_SRC" ]; then
    cp "$PA_SRC" "$FW/"
    chmod 755 "$FW/libportaudio.dylib"
    mkdir -p "$BUNDLE/Contents/Resources/_sounddevice_data/portaudio-binaries"
    cp "$PA_SRC" "$BUNDLE/Contents/Resources/_sounddevice_data/portaudio-binaries/"
fi

# Bundle libsndfile where soundfile (a librosa dependency, kept bundled
# for any future analysis plugin) expects it. py2app's 'includes' only
# pulls in _soundfile_data's __init__.py, not the .dylib sitting next to
# it, so it has to be copied by hand — same story as PortAudio above.
SF_SRC=".venv/lib/python3.13/site-packages/_soundfile_data/libsndfile_x86_64.dylib"
if [ -f "$SF_SRC" ]; then
    SF_DEST="$BUNDLE/Contents/Resources/lib/python3.13/_soundfile_data"
    mkdir -p "$SF_DEST"
    cp "$SF_SRC" "$SF_DEST/"
    chmod 755 "$SF_DEST/libsndfile_x86_64.dylib"
else
    echo "  WARNING: libsndfile.dylib not found at $SF_SRC"
fi

# Rewrite every @rpath reference
echo "Relinking binaries..."
for binary in $(find "$BUNDLE/Contents/Resources" "$FW" \( -name '*.so' -o -name '*.dylib' \) 2>/dev/null); do
    refs=$(otool -L "$binary" 2>/dev/null | grep '@rpath/' | awk '{print $1}' || true)
    for rpath in $refs; do
        libname=$(basename "$rpath")
        install_name_tool -change "$rpath" "@executable_path/../Frameworks/$libname" "$binary" 2>/dev/null || true
    done
done

# Fix install names of bundled dylibs
for f in "$FW"/*.dylib; do
    name=$(basename "$f")
    install_name_tool -id "@executable_path/../Frameworks/$name" "$f" 2>/dev/null || true
    refs=$(otool -L "$f" 2>/dev/null | grep '@rpath/' | awk '{print $1}' || true)
    for rpath in $refs; do
        libname=$(basename "$rpath")
        install_name_tool -change "$rpath" "@executable_path/../Frameworks/$libname" "$f" 2>/dev/null || true
    done
done

# Bundle Tcl/Tk script libraries (Contents/lib/ is where Tcl looks)
echo "Bundling Tcl/Tk scripts..."
TCL_SCRIPTS="$PYFW/Frameworks/Tcl.framework/Versions/8.6/Resources/Scripts"
TK_SCRIPTS="$PYFW/Frameworks/Tk.framework/Versions/8.6/Resources/Scripts"
mkdir -p "$BUNDLE/Contents/lib"
if [ -d "$TCL_SCRIPTS" ]; then
    cp -R "$TCL_SCRIPTS" "$BUNDLE/Contents/lib/tcl8.6"
fi
if [ -d "$TK_SCRIPTS" ]; then
    cp -R "$TK_SCRIPTS" "$BUNDLE/Contents/lib/tk8.6"
fi

# Verify
echo "Verifying..."
BROKEN=""
for binary in $(find "$BUNDLE/Contents/Resources" -name '*.so' 2>/dev/null); do
    refs=$(otool -L "$binary" 2>/dev/null | grep '@rpath/' || true)
    if [ -n "$refs" ]; then
        BROKEN="$BROKEN\n$binary:\n$refs"
    fi
done
if [ -n "$BROKEN" ]; then
    echo "  WARNING: remaining @rpath refs:"
    echo -e "$BROKEN"
else
    echo "  All dylibs OK"
fi

# DMG
DMG="${APP_NAME}.dmg"
rm -f "$DMG"
echo "Creating DMG..."
hdiutil create -volname "$APP_NAME" \
    -srcfolder "$BUNDLE" \
    -ov -format UDZO "$DMG" >/dev/null 2>&1

echo ""
echo "=== Done ==="
echo "  App: $BUNDLE"
echo "  DMG: ${DMG}"
