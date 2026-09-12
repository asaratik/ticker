#!/usr/bin/env bash
#
# Wrap dist/Ticker (the PyInstaller onedir) in an AppImage.
#
# Section 12.6: signing is not the trust mechanism on Linux, so there is no
# equivalent of the Windows or macOS signing dance here. AppImage is the
# format because it needs nothing installed on the target -- one executable
# file, no package manager, no runtime -- which suits a project whose other
# two platforms ship self-contained builds.
#
# appimagetool is NOT downloaded by this script. It has to be on PATH or
# named by $APPIMAGETOOL. Fetching and executing a binary from the internet
# mid-build is a supply-chain risk this project deliberately avoids, and it
# would be invisible in a build
# log. CI installs it as an explicit, auditable step instead.
#
# Usage:
#     python build.py                      # produce dist/Ticker first
#     packaging/linux/build-appimage.sh [version]
#
set -euo pipefail

VERSION="${1:-${TICKER_VERSION:-0.0.0}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$ROOT/dist"
SOURCE="$DIST/Ticker"
APPDIR="$DIST/Ticker.AppDir"
OUTPUT="$DIST/Ticker-${VERSION}-linux-x86_64.AppImage"

if [ ! -d "$SOURCE" ]; then
    echo "no $SOURCE -- run 'python build.py' first" >&2
    exit 1
fi

TOOL="${APPIMAGETOOL:-$(command -v appimagetool || true)}"
if [ -z "$TOOL" ]; then
    cat >&2 <<'MSG'
appimagetool not found.

Install it and re-run, or set APPIMAGETOOL to its path:

    wget -O appimagetool https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x appimagetool
    export APPIMAGETOOL="$PWD/appimagetool"

MSG
    exit 1
fi

echo "== staging $APPDIR =="
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"

# The whole onedir folder, not just the executable: Ticker cannot run
# without _internal beside it.
cp -a "$SOURCE/." "$APPDIR/usr/bin/"

cp "$HERE/ticker.desktop" "$APPDIR/ticker.desktop"
cp "$HERE/ticker.png" "$APPDIR/ticker.png"
# appimagetool looks for .DirIcon and falls back to guessing without it.
cp "$HERE/ticker.png" "$APPDIR/.DirIcon"

# Desktop-file integration also expects the icon under the usual hicolor
# path, which is what most launchers actually read after installation.
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "$HERE/ticker.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/ticker.png"
mkdir -p "$APPDIR/usr/share/applications"
cp "$HERE/ticker.desktop" "$APPDIR/usr/share/applications/ticker.desktop"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/usr/bin/env bash
# Entry point the AppImage runtime executes.
#
# $APPDIR is set by the runtime when the image is mounted, but not when the
# AppDir is run directly during testing, so it is derived if absent.
set -euo pipefail
SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(dirname "$SELF")"
export APPDIR="${APPDIR:-$HERE}"
export PATH="$APPDIR/usr/bin:$PATH"

# cd into the program directory: PyInstaller's onedir layout resolves
# _internal relative to the executable, and any lookup that is relative
# resolves from the working directory.
cd "$APPDIR/usr/bin"
exec "$APPDIR/usr/bin/Ticker" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

echo "== building $OUTPUT =="
rm -f "$OUTPUT"

# GitHub runners have no FUSE, and appimagetool is itself an AppImage, so it
# cannot mount itself there. Extracting instead is the documented way out.
export APPIMAGE_EXTRACT_AND_RUN="${APPIMAGE_EXTRACT_AND_RUN:-1}"

# ARCH is not inferred reliably from an AppDir with no ELF at its root.
ARCH="${ARCH:-x86_64}" "$TOOL" "$APPDIR" "$OUTPUT"

echo
echo "== AppImage =="
ls -lh "$OUTPUT"
