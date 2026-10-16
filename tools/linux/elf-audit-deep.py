#!/usr/bin/env python3
"""Second-pass audit: runtime-search-path details, dlopen-only modules,
software-rendering closure, and per-file glibc ceilings."""
import os
import re
import subprocess
import sys
from collections import defaultdict

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'squashfs-root'

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout + r.stderr

def rel(p):
    return os.path.relpath(p, ROOT)

elfs = []
for dirpath, _d, filenames in os.walk(ROOT):
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        try:
            with open(p, 'rb') as f:
                if f.read(4) == b'\x7fELF':
                    elfs.append(p)
        except OSError:
            pass

VER = re.compile(r'\b(GLIBC|GLIBCXX|CXXABI)_(\d+(?:\.\d+)+)\b')
RUNPATH = re.compile(r'\((?:RPATH|RUNPATH)\)\s+Library (?:r)?un?path: \[([^\]]*)\]')
KIND = re.compile(r'\(\(?(RPATH|RUNPATH)\)?\)')
INTERP = re.compile(r'Requesting program interpreter: ([^\]]+)\]')

print('=' * 78)
print('1) glibc ceiling per family — who needs what')
print('=' * 78)
per_file = {}
for p in elfs:
    out = run(['readelf', '--version-info', p])
    fams = defaultdict(list)
    for f, v in VER.findall(out):
        fams[f].append(tuple(int(x) for x in v.split('.')))
    if fams:
        per_file[p] = {f: max(vs) for f, vs in fams.items()}

for fam in ('GLIBC', 'GLIBCXX', 'CXXABI'):
    ranked = sorted(((d[fam], p) for p, d in per_file.items() if fam in d),
                    reverse=True)[:6]
    print(f'\n  {fam}:')
    for v, p in ranked:
        print(f'    {fam}_{".".join(map(str, v)):<8s} {rel(p)}')

print()
print('=' * 78)
print('2) dynamic search paths and loaders')
print('=' * 78)
runpath_vals = defaultdict(int)
kind_count = defaultdict(int)
interp_count = defaultdict(int)
for p in elfs:
    dyn = run(['readelf', '-d', p])
    m = RUNPATH.findall(dyn)
    for v in m:
        runpath_vals[v] += 1
    k = KIND.search(dyn)
    if k:
        kind_count[k.group(1)] += 1
    i = INTERP.search(run(['readelf', '-l', p]))
    if i:
        interp_count[i.group(1)] += 1

print(f'  loader tags: {dict(kind_count)}')
for v, n in sorted(runpath_vals.items(), key=lambda t: -t[1]):
    print(f'  {n:3d} x  runpath = {v}')
print('  interpreters:')
for v, n in sorted(interp_count.items(), key=lambda t: -t[1]):
    print(f'    {n:3d} x  {v}')

print()
print('=' * 78)
print('3) dlopen-only modules (never appear in DT_NEEDED)')
print('=' * 78)
probes = {
    'NSS crypto module': ['libsoftokn3.so', 'libsoftokn3.chk'],
    'NSS freebl':        ['libfreebl3.so', 'libfreebl3.chk'],
    'NSS dbm':           ['libnssdbm3.so', 'libnssdbm3.chk'],
    'NSS trust':         ['libnssckbi.so'],
    'nsswitch':          ['libnss_files.so.2', 'libnss_dns.so.2'],
    'p11-kit':           ['libp11-kit.so.0'],
    'GTK input modules': ['gtk-3.0/3.0.0/immodules/im-xim.so',
                          'gtk-3.0/3.0.0/immodules/im-ibus.so'],
    'GTK printbackends': ['gtk-3.0/3.0.0/printbackends'],
}
allfiles = {}
for dirpath, _d, filenames in os.walk(ROOT):
    for fn in filenames:
        allfiles.setdefault(fn, []).append(rel(os.path.join(dirpath, fn)))
for label, names in probes.items():
    for n in names:
        base = os.path.basename(n)
        hit = allfiles.get(base, [])
        print(f'  {"OK " if hit else "MISS"} {label:20s} {base:22s} {hit[:2]}')

print()
print('=' * 78)
print('4) software-rendering closure (Mesa)')
print('=' * 78)
mesa_probes = ['swrast_dri.so', 'kms_swrast_dri.so', 'libgallium', 'libLLVM',
               'libEGL.so.1', 'libGL.so.1', 'libGLX.so.0', 'libGLdispatch.so.0',
               'libgbm.so.1', 'libdrm.so.2', 'libEGL_mesa.so', '50_mesa.json']
for n in mesa_probes:
    hits = []
    for f, paths in allfiles.items():
        if n in f:
            hits.extend(paths)
    print(f'  {"OK " if hits else "MISS"} {n:22s} {hits[:3]}')

print()
print('=' * 78)
print('5) gbm / libdrm symbol requirements (UOS-era system libs lack these)')
print('=' * 78)
want = {
    'libgbm':  ['gbm_bo_create_with_modifiers2', 'gbm_bo_create_with_modifiers'],
    'libdrm':  ['drmGetFormatModifierName', 'drmGetFormatModifierVendor'],
}
for soname, syms in want.items():
    cands = [p for p in elfs if os.path.basename(p).startswith(soname)]
    for c in cands:
        out = run(['readelf', '--dyn-syms', '-W', c])
        for s in syms:
            print(f'  {"OK " if s in out else "MISS"} {rel(c):22s} defines {s}')

print()
print('=' * 78)
print('6) AppRun and hooks')
print('=' * 78)
for f in ('AppRun',):
    p = os.path.join(ROOT, f)
    if os.path.exists(p):
        print(f'--- {f} ---')
        print(open(p).read())
hp = os.path.join(ROOT, 'apprun-hooks')
if os.path.isdir(hp):
    for fn in sorted(os.listdir(hp)):
        print(f'--- apprun-hooks/{fn} ---')
        print(open(os.path.join(hp, fn)).read())
