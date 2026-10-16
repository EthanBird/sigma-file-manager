#!/usr/bin/env bash
# Repack a Tauri-built AppImage into the hardened AppImage.
#
# Adds the AppRun compatibility hook and the isolated Mesa llvmpipe closure,
# then re-runs appimagetool with the same type-2 runtime the Tauri bundler
# would have used.
#
# usage: repack-appimage.sh <extracted-AppDir> <softgl.tar.gz> <output.AppImage>
set -euo pipefail

APPDIR_IN="$1"
SOFTGL_TAR="$2"
OUT="$3"

HERE="$(cd "$(dirname "$0")" && pwd)"
export APPIMAGE_EXTRACT_AND_RUN=1
export ARCH=x86_64

echo "== [1/4] install compat hook and rewrite AppRun"
install -Dm755 "$HERE/apprun-hooks/zz-sfm-compat.sh" "$APPDIR_IN/apprun-hooks/zz-sfm-compat.sh"
cat > "$APPDIR_IN/AppRun" <<'APPRUN'
#! /usr/bin/env bash
# Patched launcher: sources the linuxdeploy GTK hook plus the compatibility
# hardening hook, then execs the application.  See apprun-hooks/zz-sfm-compat.sh.
set -e
this_dir="$(readlink -f "$(dirname "$0")")"
source "$this_dir"/apprun-hooks/"linuxdeploy-plugin-gtk.sh"
source "$this_dir"/apprun-hooks/"zz-sfm-compat.sh"
exec "$this_dir"/AppRun.wrapped "$@"
APPRUN
chmod 755 "$APPDIR_IN/AppRun"

echo "== [2/4] unpack the isolated software-rendering closure"
tar -xzf "$SOFTGL_TAR" -C "$APPDIR_IN/usr/lib"
du -sh "$APPDIR_IN/usr/lib/sfm-softgl"

echo "== [2b/4] promote load-time GL/GBM libs into usr/lib"
python3 "$HERE/promote-runtime-libs.py" --appdir "$APPDIR_IN"

echo "== [3/4] repack"
WORK="${SFM_REPACK_WORK:-/tmp/sfm-repack-work}"
rm -rf "$WORK"
mkdir -p "$WORK"
cp -a "$APPDIR_IN" "$WORK/sfm.AppDir"
rm -f "$OUT"
set +e
"$HERE/tools/appimagetool-x86_64.AppImage" \
    --runtime-file "$HERE/tools/runtime-x86_64" \
    --comp zstd \
    "$WORK/sfm.AppDir" "$OUT"
rc=$?
set -e
[ $rc -eq 0 ] || { echo "appimagetool failed (rc=$rc); work dir kept at $WORK"; exit $rc; }
rm -rf "$WORK"

echo "== [4/4] verify the produced AppImage"
ls -la "$OUT"
"$OUT" --appimage-version
"$OUT" --appimage-offset
