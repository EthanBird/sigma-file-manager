#!/usr/bin/env python3
"""Promote the load-time GL/GBM libraries from the isolated software-renderer
closure into the AppDir's main library directory.

Why this exists
---------------
Tauri's linuxdeploy bundle excludes libgbm/libdrm/libEGL from the AppImage and
expects the host to provide them.  On a host that has them (any normal desktop)
that is fine, but on a minimal or locked-down host the application does not even
reach `main()`:

    sigma-file-manager: error while loading shared libraries:
    libgbm.so.1: cannot open shared object file

`ldd` on the AppDir inside a clean Ubuntu 22.04 container reports exactly two
unresolvable shared objects: libgbm.so.1 and libEGL.so.1.  Both are ordinary
userspace libraries with stable ABIs (not kernel/vendor-driver boundary
artifacts), so carrying them is the right call — unlike copying an arbitrary
.so off a target machine, which the reference post-mortem warns against.

Placing them in usr/lib (whose ELFs all carry RUNPATH=$ORIGIN) makes them
available on every code path.  The vendor driver (libEGL_mesa.so.0) deliberately
stays in usr/lib/sfm-softgl and is still only reachable when software rendering
is selected, so hardware acceleration on a healthy host is untouched: GLVND
resolves the vendor library through the host's own vendor manifest.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

# sonames that must be resolvable before the process can start
ROOTS = ['libgbm.so.1', 'libEGL.so.1']

SKIP = re.compile(r'^(ld-linux[\w.-]*\.so[\w.-]*|libc\.so\.6|libm\.so\.6'
                  r'|libdl\.so\.2|libpthread\.so\.0|librt\.so\.1'
                  r'|libresolv\.so\.2|libstdc\+\+\.so\.6|libgcc_s\.so\.1)$')


def needed(path):
    out = subprocess.run(['readelf', '-d', path], capture_output=True, text=True).stdout
    return re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appdir', required=True)
    ap.add_argument('--source-dir', default=None,
                    help='default: <appdir>/usr/lib/sfm-softgl')
    ap.add_argument('--dest-dir', default=None,
                    help='default: <appdir>/usr/lib')
    args = ap.parse_args()

    appdir = args.appdir
    src = args.source_dir or os.path.join(appdir, 'usr/lib/sfm-softgl')
    dst = args.dest_dir or os.path.join(appdir, 'usr/lib')
    if not os.path.isdir(src):
        sys.exit(f'FATAL: {src} missing — build the software closure first')

    present = set(os.listdir(dst))
    queue = list(ROOTS)
    promoted, seen = [], set()
    while queue:
        name = queue.pop(0)
        if name in seen or SKIP.match(name) or name in present:
            continue
        seen.add(name)
        s = os.path.join(src, name)
        if not os.path.exists(s):
            print(f'  WARN: {name} not in the closure — left to the host')
            continue
        d = os.path.join(dst, name)
        os.link(s, d) if not os.path.exists(d) else None
        promoted.append(name)
        for dep in needed(s):
            if dep not in seen:
                queue.append(dep)

    print(f'promoted {len(promoted)} load-time libraries into {os.path.relpath(dst, appdir)}:')
    for p in sorted(promoted):
        print(f'  {p}')
    if not promoted:
        return 0

    print('\nre-checking with ldd (host libs only):')
    r = subprocess.run(['ldd', os.path.join(appdir, 'usr/bin/sigma-file-manager')],
                       capture_output=True, text=True)
    missing = [l.strip() for l in r.stdout.splitlines() if 'not found' in l]
    for m in missing:
        print(f'  STILL MISSING: {m}')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
