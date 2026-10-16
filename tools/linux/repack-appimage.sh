#!/usr/bin/env bash
# Repack a Tauri-built AppImage into the hardened AppImage.
# Runs on the host: needs python3, tar, squashfs-tools (or appimagetool's bundled one).
set -euo pipefail

APPDIR="$1"        # extracted AppDir (squashfs-root)
SOFTGL_TAR="$2"    # sfm-softgl tarball produced inside the 22.04 container
OUT="$3"           # output .AppImage

HERE="$(cd "$(dirname "$0")" && pwd)"
export APPIMAGE_EXTRACT_AND_RUN=1
export ARCH=x86_64

echo "== [1/4] install compat hook and rewrite AppRun"
install -Dm755 "$HERE/apprun-hooks/zz-sfm-compat.sh" "$APPDIR/apprun-hooks/zz-sfm-compat.sh"
cat > "$APPDIR/AppRun" <<'APPRUN'
#! /usr/bin/env bash
# Patched launcher: sources the linuxdeploy GTK hook plus the compatibility
# hardening hook, then execs the application.  See apprun-hooks/zz-sfm-compat.sh.
set -e
this_dir="$(readlink -f "$(dirname "$0")")"
source "$this_dir"/apprun-hooks/"linuxdeploy-plugin-gtk.sh"
source "$this_dir"/apprun-hooks/"zz-sfm-compat.sh"
exec "$this_dir"/AppRun.wrapped "$@"
APPRUN
chmod 755 "$APPDIR/AppRun"

echo "== [2/4] unpack the isolated software-rendering closure"
tar -xzf "$SOFTGL_TAR" -C "$APPDIR/usr/lib"
du -sh "$APPDIR/usr/lib/sfm-softgl"
ls "$APPDIR/usr/lib/sfm-softgl" | head -20
ls "$APPDIR/usr/lib/sfm-softgl/dri"

echo "== [3/4] repack"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mv "$APPDIR" "$WORK/sfm.AppDir"
rm -f "$OUT"
"$HERE/tools/appimagetool-x86_64.AppImage" \
    --runtime-file "$HERE/tools/runtime-x86_64" \
    --comp xz \
    "$WORK/sfm.AppDir" "$OUT" 2>&1 | tail -15

echo "== [4/4] verify the produced AppImage"
ls -la "$OUT"
"$OUT" --appimage-version || true
"$OUT" --appimage-offset
