#!/usr/bin/env python3
"""Measure the real ABI requirements of an AppImage AppDir.

Reports, for every ELF in the tree:
  - max GLIBC_x.y required by undefined symbols
  - max GLIBCXX_x.y / CXXABI_x.y
  - DT_NEEDED sonames that resolve nowhere inside the AppDir
  - PT_INTERP (dynamic loader) value
  - whether DT_RPATH / DT_RUNPATH exists
"""
import os
import re
import subprocess
import sys
from collections import defaultdict

ROOT = sys.argv[1] if len(sys.argv) > 1 else 'squashfs-root'

libdirs = set()
elfs = []
for dirpath, _dirnames, filenames in os.walk(ROOT):
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        try:
            with open(p, 'rb') as f:
                if f.read(4) != b'\x7fELF':
                    continue
        except OSError:
            continue
        elfs.append(p)
        if fn.endswith('.so') or '.so.' in fn:
            libdirs.add(dirpath)

def ver_key(v):
    return tuple(int(x) for x in v.split('.'))

def readelf(args, path):
    r = subprocess.run(['readelf', *args, path], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ''

VER_RE = re.compile(r'\b(GLIBC|GLIBCXX|CXXABI)_(\d+(?:\.\d+)+)\b')

have_soname = set()
for d in list(libdirs):
    for fn in os.listdir(d):
        have_soname.add(fn)

summary = defaultdict(list)
overall = defaultdict(lambda: (0, 0))
interp_counts = defaultdict(int)
no_rpath = []
runpath_used = []
missing_needed = defaultdict(set)
rpath_values = defaultdict(int)

for p in sorted(elfs):
    out = readelf(['--version-info'], p)
    for family, ver in VER_RE.findall(out):
        k = ver_key(ver)
        overall[family] = max(overall[family], k) if overall[family] else k
    for family in ('GLIBC', 'GLIBCXX', 'CXXABI'):
        vs = [ver_key(v) for f, v in VER_RE.findall(out) if f == family]
        if vs:
            summary[family].append((max(vs), p))

    dyn = readelf(['-d'], p)
    needed = re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', dyn)
    missing = [n for n in needed if n not in have_soname]
    if missing:
        missing_needed[p] = missing

    interp = re.search(r'Requesting program interpreter: ([^\]]+)\]', readelf(['-l'], p))
    if interp:
        interp_counts[interp.group(1)] += 1

    rpath = re.findall(r'\((?:RPATH|RUNPATH)\)\s+Library rpath: \[([^\]]*)\]', dyn)
    for r in rpath:
        rpath_values[r] += 1
    if re.search(r'\(RUNPATH\)', dyn):
        runpath_used.append(p)
    if interp and not rpath:
        no_rpath.append(p)

print(f'ELF files: {len(elfs)}')
print(f'library dirs: {len(libdirs)}  sonames available: {len(have_soname)}')
print()
print('=== highest versioned symbol requirement ===')
for fam in ('GLIBC', 'GLIBCXX', 'CXXABI'):
    if summary[fam]:
        worst = max(summary[fam], key=lambda t: t[0])
        print(f'  {fam:8s} max {fam}_{".".join(map(str, worst[0])):>7s}  ({worst[1]})')
    else:
        print(f'  {fam:8s} none')
print()
print('=== loader (PT_INTERP) ===')
for k, v in sorted(interp_counts.items(), key=lambda t: -t[1]):
    print(f'  {v:3d}  {k}')
print()
print(f'=== DT_NEEDED not resolvable inside AppDir ===')
if missing_needed:
    union = defaultdict(list)
    for p, ms in missing_needed.items():
        for m in ms:
            union[m].append(os.path.relpath(p, ROOT))
    for m, users in sorted(union.items(), key=lambda t: -len(t[1])):
        print(f'  {m}   <- {len(users)} ELF(s), e.g. {users[0]}')
else:
    print('  none')
print()
print(f'=== DT_RPATH values ===')
for k, v in sorted(rpath_values.items(), key=lambda t: -t[1])[:10]:
    print(f'  {v:3d}  {k or "(empty)"}')
print()
print(f'RUNPATH used (not RPATH): {len(runpath_used)}')
print(f'ELF with interpreter but no RPATH: {len(no_rpath)}')
for p in no_rpath[:10]:
    print(f'   {os.path.relpath(p, ROOT)}')
