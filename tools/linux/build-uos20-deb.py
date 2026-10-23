#!/usr/bin/env python3
"""Build a UOS Desktop 20 / Deepin 20.8 (glibc 2.28) compatible .deb package
from the hardened Sigma File Manager AppImage (built on Ubuntu 22.04 / glibc 2.35).

Why a fixed-root .deb is required for glibc 2.28
------------------------------------------------
An AppImage mounts at a random `/tmp/.mount_<id>` path on every run, and the
Linux kernel does not expand `$ORIGIN` inside `PT_INTERP`.  Even if `AppRun`
invokes a bundled `ld-linux-x86-64.so.2` directly for the main executable,
WebKitGTK spawns `WebKitNetworkProcess` and `WebKitWebProcess` via `execve()`,
whose `PT_INTERP` still points at the host's `/lib64/ld-linux-x86-64.so.2`
(glibc 2.28 on UOS 20), which immediately fails with missing `GLIBC_2.32`..`2.35`
and `GLIBCXX_3.4.30` symbols.

Installing into a fixed root (`/opt/sigma-file-manager-uos20`) solves this:
  1. Every executable (`sigma-file-manager`, `WebKitNetworkProcess`,
     `WebKitWebProcess`) has its `PT_INTERP` rewritten to
     `/opt/sigma-file-manager-uos20/uos-runtime/ld-linux-x86-64.so.2`.
  2. Every non-glibc ELF in the bundle has its dynamic search path converted
     to a transitive `DT_RPATH` (`--force-rpath`) covering `uos-runtime`,
     `usr/lib`, and `usr/lib/sfm-softgl`.
  3. The launcher script explicitly unsets `LD_LIBRARY_PATH` so host child
     processes spawned by the file manager (`xdg-open`, `which`, `gsettings`,
     `kreadconfig5`, user terminals) run under the host's glibc 2.28 loader
     and libraries without foreign-libc crashes.
  4. `GIO_MODULE_DIR` is pinned to the bundled GIO modules directory so host
     `/usr/lib/x86_64-linux-gnu/gio/modules/libgvfsdbus.so` (compiled against
     UOS 20's older GLib) is never loaded into the process.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys

INSTALL_ROOT = '/opt/sigma-file-manager-uos20'
RUNTIME_DIR = f'{INSTALL_ROOT}/uos-runtime'
INTERPRETER = f'{RUNTIME_DIR}/ld-linux-x86-64.so.2'
RPATH = ':'.join([
    RUNTIME_DIR,
    f'{INSTALL_ROOT}/usr/lib',
    f'{INSTALL_ROOT}/usr/lib/sfm-softgl',
])

SYS_LIB_DIRS = [
    '/lib/x86_64-linux-gnu',
    '/usr/lib/x86_64-linux-gnu',
    '/lib64',
    '/usr/lib64',
]

# Core glibc objects that must be copied verbatim into uos-runtime and NEVER
# modified by patchelf (rewriting their dynamic sections breaks GLIBC_PRIVATE).
GLIBC_CORE = [
    'ld-linux-x86-64.so.2',
    'libc.so.6',
    'libm.so.6',
    'libdl.so.2',
    'libpthread.so.0',
    'librt.so.1',
    'libresolv.so.2',
    'libutil.so.1',
    'libanl.so.1',
    'libBrokenLocale.so.1',
    'libnss_files.so.2',
    'libnss_dns.so.2',
    'libnss_compat.so.2',
]

# Base C++ / compiler runtime libraries always seeded into uos-runtime.
BASE_RUNTIME_SEEDS = GLIBC_CORE + [
    'libstdc++.so.6',
    'libgcc_s.so.1',
]


def sh(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def check_build_environment():
    glibc = sh(['getconf', 'GNU_LIBC_VERSION']).stdout.strip()
    if glibc != 'glibc 2.35':
        sys.exit(f'FATAL: build host must be glibc 2.35 (Ubuntu 22.04), got {glibc!r}')

    pe = sh(['patchelf', '--version']).stdout.strip()
    m = re.search(r'(\d+)\.(\d+)', pe)
    if not m or (int(m.group(1)), int(m.group(2))) < (0, 14):
        sys.exit(
            f'FATAL: patchelf >= 0.14 required (older versions corrupt .dynamic '
            f'and drop DT_NEEDED on linuxdeploy binaries); got {pe!r}'
        )
    print(f'[uos20-deb] build host verified: {glibc}, {pe}')


def ldconfig_map():
    ldconfig = shutil.which('ldconfig') or '/sbin/ldconfig'
    out = sh([ldconfig, '-p']).stdout
    mapping = {}
    for line in out.splitlines():
        if '=>' not in line:
            continue
        left, right = (p.strip() for p in line.split('=>', 1))
        soname = left.split()[0]
        if 'x86-64' in left and os.path.exists(right):
            mapping.setdefault(soname, right)
    return mapping


def find_system_lib(soname, ldmap):
    if soname in ldmap and os.path.exists(ldmap[soname]):
        return os.path.realpath(ldmap[soname])
    for d in SYS_LIB_DIRS:
        cand = os.path.join(d, soname)
        if os.path.exists(cand):
            return os.path.realpath(cand)
    return None


def is_elf(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'\x7fELF'
    except OSError:
        return False


def elf_needed(path):
    out = sh(['readelf', '-d', path], check=False).stdout
    return re.findall(r'\(NEEDED\)\s+(?:Shared library|共享库)[:：]\s*\[([^\]]+)\]', out)


def elf_interp(path):
    out = sh(['readelf', '-l', path], check=False).stdout
    m = re.search(r'Requesting program interpreter: ([^\]]+)\]', out)
    return m.group(1).strip() if m else None


def collect_elfs(root):
    res = []
    for dp, _, fns in os.walk(root):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            if is_elf(p):
                res.append(p)
    return res


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appimage', required=True, help='Input hardened v2.2.1-zh.2 AppImage')
    ap.add_argument('--out', required=True, help='Output .deb file path')
    ap.add_argument('--pkg-version', default='2.2.1-zh.2+uos20.1',
                    help='Debian package version string')
    ap.add_argument('--work-dir', default='/var/tmp/sfm-uos20-deb-work')
    args = ap.parse_args()

    check_build_environment()

    work = os.path.abspath(args.work_dir)
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    print('[uos20-deb] [1/6] extracting AppImage...')
    extract_dir = os.path.join(work, 'extract')
    os.makedirs(extract_dir)
    subprocess.run(
        [os.path.abspath(args.appimage), '--appimage-extract'],
        cwd=extract_dir,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    squashfs = os.path.join(extract_dir, 'squashfs-root')

    pkg_root = os.path.join(work, 'pkg')
    opt_root = os.path.join(pkg_root, INSTALL_ROOT.lstrip('/'))
    os.makedirs(os.path.dirname(opt_root), exist_ok=True)
    shutil.move(squashfs, opt_root)

    # Remove the bundled Ubuntu 22.04 /usr/bin/xdg-open script so xdg-open calls
    # fall through to the UOS 20 host's /usr/bin/xdg-open.
    bundled_xdg_open = os.path.join(opt_root, 'usr/bin/xdg-open')
    if os.path.exists(bundled_xdg_open):
        os.remove(bundled_xdg_open)

    print('[uos20-deb] [2/6] collecting private glibc 2.35 + transitive DT_NEEDED closure...')
    rt_dir = os.path.join(opt_root, 'uos-runtime')
    os.makedirs(rt_dir, exist_ok=True)
    ldmap = ldconfig_map()

    existing_sonames = set()
    for d in (
        os.path.join(opt_root, 'usr/lib'),
        os.path.join(opt_root, 'usr/lib/sfm-softgl'),
    ):
        if os.path.isdir(d):
            existing_sonames.update(os.listdir(d))

    queue = list(BASE_RUNTIME_SEEDS)
    for elf in collect_elfs(opt_root):
        for dep in elf_needed(elf):
            if dep not in existing_sonames and dep not in queue:
                queue.append(dep)

    copied_runtime = []
    seen = set()
    while queue:
        soname = queue.pop(0)
        if soname in seen:
            continue
        seen.add(soname)
        if soname in existing_sonames and soname not in BASE_RUNTIME_SEEDS:
            continue
        src = find_system_lib(soname, ldmap)
        if not src:
            sys.exit(f'FATAL: required soname {soname!r} not found on build host')
        dst = os.path.join(rt_dir, soname)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755 if soname.startswith('ld-linux') or soname == 'libc.so.6' else 0o644)
        copied_runtime.append(soname)
        for dep in elf_needed(dst):
            if dep not in seen and dep not in existing_sonames:
                queue.append(dep)

    print(f'[uos20-deb] collected {len(copied_runtime)} runtime objects into uos-runtime/')

    print('[uos20-deb] [3/6] rewriting PT_INTERP and transitive DT_RPATH with patchelf >= 0.14...')
    glibc_core_set = set(GLIBC_CORE)
    patched_elfs = []
    interp_elfs = []

    for elf in collect_elfs(opt_root):
        rel = os.path.relpath(elf, opt_root)
        base = os.path.basename(elf)
        if base in glibc_core_set:
            continue

        before_needed = elf_needed(elf)
        interp = elf_interp(elf)
        is_shared_lib = '.so' in base

        cmd = ['patchelf', '--force-rpath', '--set-rpath', RPATH]
        if interp and not is_shared_lib:
            cmd.extend(['--set-interpreter', INTERPRETER])
            interp_elfs.append(rel)
        cmd.append(elf)
        sh(cmd)

        after_needed = elf_needed(elf)
        if before_needed != after_needed:
            sys.exit(
                f'FATAL: patchelf altered DT_NEEDED on {rel}: '
                f'{before_needed} -> {after_needed}'
            )
        patched_elfs.append(rel)

    print(
        f'[uos20-deb] patched {len(patched_elfs)} ELFs '
        f'({len(interp_elfs)} executable interpreters set to {INTERPRETER})'
    )

    print('[uos20-deb] [4/6] auditing closure completeness...')
    available = set(os.listdir(rt_dir)) | existing_sonames
    unresolved = {}
    for elf in collect_elfs(opt_root):
        missing = [n for n in elf_needed(elf) if n not in available]
        if missing:
            unresolved[os.path.relpath(elf, opt_root)] = missing
    if unresolved:
        sys.exit(f'FATAL: unresolved DT_NEEDED in bundle: {json.dumps(unresolved, indent=2)}')

    manifest = {
        'schemaVersion': 1,
        'target': 'uos20-amd64',
        'packageVersion': args.pkg_version,
        'minimumSystemGlibc': '2.28',
        'privateGlibcVersion': '2.35',
        'installRoot': INSTALL_ROOT,
        'privateInterpreter': INTERPRETER,
        'privateRpath': RPATH,
        'renderingDefault': 'software (Mesa llvmpipe in usr/lib/sfm-softgl)',
        'runtimeObjects': sorted(copied_runtime),
        'patchedElfCount': len(patched_elfs),
        'interpreterElfs': sorted(interp_elfs),
    }
    with open(os.path.join(opt_root, 'uos-runtime-manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print('[uos20-deb] [5/6] writing UOS 20 launcher, desktop entry and icons...')
    launcher_Code = f"""#!/usr/bin/env bash
# Sigma File Manager launcher for UOS Desktop 20 / Deepin 20.8 (glibc 2.28).
#
# Uses the fixed-root private glibc 2.35 interpreter and transitive DT_RPATH
# embedded in every bundled ELF.  LD_LIBRARY_PATH is deliberately unset so that
# child processes spawned by the file manager (xdg-open, which, gsettings,
# user terminals) execute cleanly against the host's glibc 2.28 libraries.
set -euo pipefail

APPDIR="{INSTALL_ROOT}"
export APPDIR

sfm_debug() {{
    [ "${{SFM_DEBUG:-0}}" = "1" ] && printf 'sfm-uos20: %s\\n' "$*" >&2
    return 0
}}

# Never export LD_LIBRARY_PATH into host child processes.
unset LD_LIBRARY_PATH

# Confine GTK 3, GDK-Pixbuf, GIO modules and GSettings schemas to the bundle.
export GTK_DATA_PREFIX="$APPDIR"
export GTK_THEME="${{APPIMAGE_GTK_THEME:-Adwaita:dark}}"
export GDK_BACKEND=x11
export XDG_DATA_DIRS="$APPDIR/usr/share:/usr/share${{XDG_DATA_DIRS:+:$XDG_DATA_DIRS}}"
export GSETTINGS_SCHEMA_DIR="$APPDIR/usr/share/glib-2.0/schemas"
export GI_TYPELIB_PATH="$APPDIR/usr/lib/girepository-1.0"
export GTK_EXE_PREFIX="$APPDIR/usr"
export GTK_PATH="$APPDIR/usr/lib/gtk-3.0"
export GTK_IM_MODULE_FILE="$APPDIR/usr/lib/gtk-3.0/3.0.0/immodules.cache"
export GDK_PIXBUF_MODULE_FILE="$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
# GIO_MODULE_DIR (not just GIO_EXTRA_MODULES) prevents GLib from dlopening host
# /usr/lib/x86_64-linux-gnu/gio/modules/libgvfsdbus.so built against older GLib.
export GIO_MODULE_DIR="$APPDIR/usr/lib/x86_64-linux-gnu/gio/modules"
export GIO_EXTRA_MODULES="$APPDIR/usr/lib/x86_64-linux-gnu/gio/modules"
export GST_PLUGIN_SYSTEM_PATH_1_0="$APPDIR/usr/lib/gstreamer-1.0"

if [ "${{SFM_KEEP_GTK_MODULES:-0}}" != "1" ]; then
    sfm_debug "clearing session GTK_MODULES='${{GTK_MODULES:-}}' GTK3_MODULES='${{GTK3_MODULES:-}}'"
    unset GTK_MODULES GTK3_MODULES
fi

if [ "${{SFM_KEEP_AT_BRIDGE:-0}}" != "1" ]; then
    export NO_AT_BRIDGE=1
fi

if [ "${{SFM_ISOLATED_INPUT:-0}}" = "1" ]; then
    export GTK_IM_MODULE=gtk-im-context-simple
    export GDK_CORE_DEVICE_EVENTS=1
    sfm_debug "isolated input enabled: GTK_IM_MODULE=gtk-im-context-simple GDK_CORE_DEVICE_EVENTS=1"
fi

# Default to the bundled Mesa llvmpipe closure on UOS 20 (host Mesa/DRI ABI
# predates Ubuntu 22.04 WebKitGTK 2.50.4).  Set SFM_GL=hardware to opt out.
SFM_GL_MODE="${{SFM_GL:-software}}"
if [ "$SFM_GL_MODE" = "software" ]; then
    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER="${{SFM_GALLIUM_DRIVER:-llvmpipe}}"
    export LIBGL_DRIVERS_PATH="$APPDIR/usr/lib/sfm-softgl/dri"
    export __EGL_VENDOR_LIBRARY_FILENAMES="$APPDIR/usr/lib/sfm-softgl/50_mesa.json"
    export WEBKIT_DISABLE_COMPOSITING_MODE=1
    export WEBKIT_DISABLE_DMABUF_RENDERER=1
    export WEBKIT_DMABUF_RENDERER_DISABLE_GBM=1
    export LIBGL_DRI3_DISABLE=1
    export SFM_GL_ACTIVE=software
    sfm_debug "software GL enabled via $APPDIR/usr/lib/sfm-softgl"
else
    export WEBKIT_DISABLE_COMPOSITING_MODE=1
    export SFM_GL_ACTIVE=hardware
    sfm_debug "hardware GL requested"
fi

# WebKitNetworkProcess / WebKitWebProcess paths inside libwebkit2gtk-4.1.so.0
# were patched by linuxdeploy to relative '././/lib/x86_64-linux-gnu/...',
# requiring cwd to be $APPDIR/usr when spawning helpers.
cd "$APPDIR/usr"
exec "$APPDIR/usr/bin/sigma-file-manager" "$@"
"""
    opt_apprun = os.path.join(opt_root, 'AppRun')
    with open(opt_apprun, 'w', encoding='utf-8', newline='\n') as f:
        f.write(launcher_Code)
    os.chmod(opt_apprun, 0o755)

    usr_bin_dir = os.path.join(pkg_root, 'usr/bin')
    os.makedirs(usr_bin_dir, exist_ok=True)
    usr_bin_launcher = os.path.join(usr_bin_dir, 'sigma-file-manager')
    with open(usr_bin_launcher, 'w', encoding='utf-8', newline='\n') as f:
        f.write(launcher_Code)
    os.chmod(usr_bin_launcher, 0o755)

    apps_dir = os.path.join(pkg_root, 'usr/share/applications')
    os.makedirs(apps_dir, exist_ok=True)
    desktop_entry = """[Desktop Entry]
Categories=System;FileTools;FileManager;Utility;
Comment=Modern file manager for Linux (UOS 20 hardened build)
Comment[zh_CN]=现代开源文件管理器（统信 UOS 20 兼容硬化版）
Exec=/usr/bin/sigma-file-manager %U
Icon=sigma-file-manager
Name=Sigma File Manager
Name[zh_CN]=Sigma 文件管理器
StartupWMClass=sigma-file-manager
Terminal=false
Type=Application
"""
    with open(os.path.join(apps_dir, 'sigma-file-manager.desktop'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(desktop_entry)

    # Copy application icons to /usr/share/icons/hicolor
    src_icons = os.path.join(opt_root, 'usr/share/icons/hicolor')
    dst_icons = os.path.join(pkg_root, 'usr/share/icons/hicolor')
    if os.path.isdir(src_icons):
        shutil.copytree(src_icons, dst_icons, dirs_exist_ok=True)

    # Compute Installed-Size in KiB
    total_bytes = 0
    for dp, _, fns in os.walk(pkg_root):
        for fn in fns:
            p = os.path.join(dp, fn)
            if not os.path.islink(p):
                total_bytes += os.path.getsize(p)
    installed_kb = (total_bytes + 1023) // 1024

    debian_dir = os.path.join(pkg_root, 'DEBIAN')
    os.makedirs(debian_dir, exist_ok=True)
    control = f"""Package: sigma-file-manager
Version: {args.pkg_version}
Section: utils
Priority: optional
Architecture: amd64
Installed-Size: {installed_kb}
Depends: libc6 (>= 2.28), xdg-utils
Maintainer: EthanBird <crystal0xff@gmail.com>
Homepage: https://github.com/EthanBird/sigma-file-manager
Description: Sigma File Manager (Simplified Chinese & UOS Desktop 20 hardened edition)
 Free, open-source, modern file manager app for Linux.
 .
 This package installs into {INSTALL_ROOT} with a self-contained
 Ubuntu 22.04 (glibc 2.35 / GLIBCXX_3.4.30) private runtime closure,
 bundled WebKitGTK 4.1, and an isolated Mesa llvmpipe software rendering
 stack so it runs out-of-the-box on UOS Desktop 20 (1070) / Deepin 20.8
 (glibc 2.28) without polluting host child processes.
"""
    with open(os.path.join(debian_dir, 'control'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(control)

    # Normalize permissions for dpkg-deb
    for dp, dns, fns in os.walk(pkg_root):
        os.chmod(dp, 0o755)
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p):
                continue
            mode = stat.S_IMODE(os.stat(p).st_mode)
            if mode & 0o111:
                os.chmod(p, 0o755)
            else:
                os.chmod(p, 0o644)

    print(f'[uos20-deb] [6/6] building {args.out}...')
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    subprocess.run(
        ['dpkg-deb', '--root-owner-group', '-Zxz', '-z6', '--build', pkg_root, os.path.abspath(args.out)],
        check=True,
    )
    digest = sha256_file(args.out)
    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f'[uos20-deb] SUCCESS: {args.out} ({size_mb:.2f} MB, sha256={digest})')


if __name__ == '__main__':
    main()
