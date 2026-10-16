#!/usr/bin/env python3
"""Collect an isolated Mesa software-rendering closure and tar it up.

Must run inside the Ubuntu 22.04 build container: the point is to ship a
*fixed, known-good* GL stack whose only external requirement is the same C
runtime the AppImage already needs (glibc 2.35 / libstdc++ of the build base).

Layout produced inside the tarball:

    sfm-softgl/50_mesa.json
    sfm-softgl/libEGL.so.1, libGL.so.1, libGLX.so.0, libGLdispatch.so.0, ...
    sfm-softgl/libgbm.so.1, libdrm.so.2, libEGL_mesa.so.0, libLLVM-*.so.1, ...
    sfm-softgl/dri/swrast_dri.so, kms_swrast_dri.so
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

SYS_LIB_DIRS = [
    '/usr/lib/x86_64-linux-gnu',
    '/lib/x86_64-linux-gnu',
    '/usr/lib64',
    '/usr/lib',
]

# Owned by the target's C runtime — never vendor these.
SKIP = re.compile(
    r'^(ld-linux[\w.-]*\.so[\w.-]*'
    r'|libc\.so\.6|libm\.so\.6|libdl\.so\.2|libpthread\.so\.0|librt\.so\.1'
    r'|libresolv\.so\.2|libstdc\+\+\.so\.6|libgcc_s\.so\.1)$'
)

GLVND = ['libEGL.so.1', 'libGL.so.1', 'libGLX.so.0', 'libGLdispatch.so.0',
         'libOpenGL.so.0']
GBM = ['libgbm.so.1', 'libdrm.so.2']
MESA_EGL = ['libEGL_mesa.so.0']
DRI = ['swrast_dri.so', 'kms_swrast_dri.so']
VENDOR_JSON = '/usr/share/glvnd/egl_vendor.d/50_mesa.json'


def ldconfig_map():
    out = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True).stdout
    m = {}
    for line in out.splitlines():
        if '=>' not in line:
            continue
        soname, path = (p.strip() for p in line.split('=>', 1))
        m.setdefault(soname, path)
    return m


def needed(path):
    out = subprocess.run(['readelf', '-d', path], capture_output=True, text=True).stdout
    return re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', out)


def find(name, ldmap):
    if name in ldmap and os.path.exists(ldmap[name]):
        return ldmap[name]
    for d in SYS_LIB_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    for d in SYS_LIB_DIRS:
        p = os.path.join(d, 'dri', name)
        if os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='output tarball')
    ap.add_argument('--stage', default='/tmp/sfm-softgl-stage')
    ap.add_argument('--appdir-libs', default='',
                    help='optional AppDir usr/lib dir; sonames already there are skipped')
    args = ap.parse_args()

    if not os.path.exists(VENDOR_JSON):
        sys.exit(f'FATAL: {VENDOR_JSON} missing — install libegl-mesa0')
    for soname in GBM + MESA_EGL + GLVND:
        if find(soname, ldconfig_map()) is None:
            sys.exit(f'FATAL: {soname} not present in the build container')

    already = set()
    if args.appdir_libs and os.path.isdir(args.appdir_libs):
        already = set(os.listdir(args.appdir_libs))

    ldmap = ldconfig_map()
    stage = args.stage
    if os.path.exists(stage):
        shutil.rmtree(stage)
    libdir = os.path.join(stage, 'sfm-softgl')
    dridir = os.path.join(libdir, 'dri')
    os.makedirs(dridir)

    roots = GLVND + GBM + MESA_EGL
    seen, queue, copied = set(), list(roots), []
    for d in DRI:
        p = os.path.join(SYS_LIB_DIRS[0], 'dri', d)
        if os.path.exists(p):
            queue.append(d)
            break  # swrast is the entry point; kms_swrast is optional

    while queue:
        name = queue.pop(0)
        if name in seen or SKIP.match(name) or name in already:
            continue
        seen.add(name)
        src = find(name, ldmap)
        if src is None:
            print(f'  unresolved (kept for the host): {name}')
            continue
        dst = os.path.join(dridir if '/dri/' in src else libdir, os.path.basename(src))
        shutil.copy2(src, dst)
        copied.append(os.path.relpath(dst, stage))
        for dep in needed(src):
            if dep not in seen:
                queue.append(dep)

    shutil.copy2(VENDOR_JSON, os.path.join(libdir, '50_mesa.json'))

    out = args.out
    subprocess.run(['tar', '-C', stage, '-czf', out, 'sfm-softgl'], check=True)

    print(f'closure: {len(copied)} libraries, tarball {os.path.getsize(out)/1e6:.1f} MB')
    for c in sorted(copied):
        print(f'  {c}')
    print('  sfm-softgl/50_mesa.json')


if __name__ == '__main__':
    main()
