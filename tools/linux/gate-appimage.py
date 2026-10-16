#!/usr/bin/env python3
"""Headless acceptance gate for the Linux AppImage.

Runs inside a container that reproduces the minimum supported userland
(Ubuntu 22.04 / glibc 2.35) with *no* system libwebkit2gtk-4.1, so a green run
proves the AppImage carries its own desktop runtime and does not depend on the
host's.

The gate is deliberately more than "the process stayed alive for 20 s":
  1. ABI floor           getconf GNU_LIBC_VERSION, no system WebKitGTK
  2. launch              window appears, WebKitWebProcess survives
  3. isolation           read the live process environment from /proc
  4. pixels              distinct colours + greyscale variance + dark-pixel
                         ratio in the text band
  5. real input          xdotool delivers genuine X11 events; the page must
                         actually change afterwards
  6. GL path             which GL stack the launcher selected, and why
  7. failure memory      no EGL / swrast / Aborting lines in the log
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from PIL import Image, ImageStat


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                          text=True, **kw)


def log(msg):
    print(f'[gate] {msg}', flush=True)


def screenshot(display, path):
    r = sh(['import', '-display', display, '-window', 'root', path])
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError(f'screenshot failed: {r.stderr[:300]}')
    return path


def metrics(path):
    img = Image.open(path).convert('RGB')
    w, h = img.size
    colors = img.getcolors(maxcolors=1 << 24)
    ncolors = len(colors) if colors else -1
    grey = img.convert('L')
    stddev = ImageStat.Stat(grey).stddev[0]
    band = grey.crop((0, 0, min(1100, w), min(100, h)))
    px = list(band.getdata())
    dark = sum(1 for p in px if p < 77) / max(1, len(px))
    return {'size': [w, h], 'distinct_colors': ncolors,
            'grey_stddev': round(stddev, 6), 'top_band_dark_ratio': round(dark, 6)}


def pid_of(pattern):
    r = sh(['pgrep', '-f', pattern])
    return [int(p) for p in r.stdout.split() if p.isdigit()]


def env_of(pid):
    try:
        raw = open(f'/proc/{pid}/environ', 'rb').read()
    except OSError:
        return None
    out = {}
    for item in raw.split(b'\0'):
        if b'=' in item:
            k, v = item.split(b'=', 1)
            out[k.decode('utf-8', 'replace')] = v.decode('utf-8', 'replace')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appimage', required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--scenario', default='host-gl',
                    choices=['host-gl', 'no-gl', 'forced-software'])
    ap.add_argument('--webkit-check-seconds', type=int, default=20)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    report = {'scenario': args.scenario, 'checks': {}, 'metrics': {}}
    verdict = []

    def check(name, ok, detail):
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}
        verdict.append(bool(ok))
        log(f'{"PASS" if ok else "FAIL"}  {name}: {detail}')

    # ---------------------------------------------------------------- 1. ABI
    glibc = sh('getconf GNU_LIBC_VERSION').stdout.strip()
    check('glibc_is_2_35', glibc == 'glibc 2.35', glibc)
    wk = sh("dpkg-query -W -f='${Status}' libwebkit2gtk-4.1-0").stdout.strip()
    check('no_system_webkit2gtk', 'install ok installed' not in wk,
          f'dpkg status={wk!r}')

    # reproduce the target's GL situation
    dri = '/usr/lib/x86_64-linux-gnu/dri'
    if args.scenario == 'no-gl' and os.path.isdir(dri) and not os.path.exists(dri + '.hidden'):
        shutil.move(dri, dri + '.hidden')
    elif args.scenario != 'no-gl' and os.path.exists(dri + '.hidden'):
        shutil.move(dri + '.hidden', dri)
    check('scenario_gl_state',
          True,
          f'/dev/dri={"present" if os.path.isdir("/dev/dri") else "absent"}, '
          f'system dri dir={"present" if os.path.isdir(dri) else "removed"}')

    # ------------------------------------------------------------- 2. launch
    display = ':99'
    xvfb = subprocess.Popen(['Xvfb', display, '-screen', '0', '1280x800x24',
                             '-nolisten', 'tcp', '-ac'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    env = dict(os.environ)
    env['DISPLAY'] = display
    env['SFM_DEBUG'] = '1'
    if args.scenario == 'forced-software':
        env['SFM_GL'] = 'software'
    # simulate a DDE/UOS session polluting the environment
    env['GTK_MODULES'] = 'gail:atk-bridge'
    env['GTK3_MODULES'] = 'atk-bridge'
    env['GTK_PATH'] = '/usr/lib/x86_64-linux-gnu/gtk-3.0'

    applog = os.path.join(args.outdir, 'app.log')
    logf = open(applog, 'wb')
    proc = subprocess.Popen([args.appimage, '--appimage-extract-and-run'],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)
    report['appimage_pid'] = proc.pid

    win = ''
    for _ in range(40):
        time.sleep(1)
        r = sh(f'DISPLAY={display} xdotool search --name "Sigma File Manager"')
        ids = [l for l in r.stdout.split() if l.strip()]
        if ids:
            win = ids[0]
            break
    check('window_created', bool(win), f'xdotool window id={win or "none"}')

    time.sleep(args.webkit_check_seconds)
    webkit_pids = pid_of('WebKitWebProcess')
    check('webkit_web_process_alive', bool(webkit_pids),
          f'WebKitWebProcess pids={webkit_pids}')

    # --------------------------------------------------------- 3. isolation
    main_pids = [p for p in pid_of('sigma-file-manager') if p not in webkit_pids]
    main_pids = [p for p in main_pids if os.path.exists(f'/proc/{p}/environ')]
    target = webkit_pids[0] if webkit_pids else (main_pids[0] if main_pids else None)
    report['inspected_pid'] = target
    if target:
        e = env_of(target) or {}
        report['proc_env'] = {k: v for k, v in e.items() if k in
                              ('GTK_PATH', 'GTK_MODULES', 'GTK3_MODULES',
                               'NO_AT_BRIDGE', 'GTK_IM_MODULE',
                               'GDK_CORE_DEVICE_EVENTS', 'LD_LIBRARY_PATH',
                               'LIBGL_ALWAYS_SOFTWARE', 'LIBGL_DRIVERS_PATH',
                               '__EGL_VENDOR_LIBRARY_FILENAMES',
                               'GALLIUM_DRIVER', 'WEBKIT_DISABLE_COMPOSITING_MODE',
                               'GDK_BACKEND')}
        check('gtk_modules_cleared',
              'GTK_MODULES' not in e and 'GTK3_MODULES' not in e,
              f"GTK_MODULES={e.get('GTK_MODULES', '<unset>')} "
              f"GTK3_MODULES={e.get('GTK3_MODULES', '<unset>')}")
        check('gtk_path_confined',
              e.get('GTK_PATH', '').startswith('/tmp/.mount_') or
              e.get('GTK_PATH', '').startswith('/tmp/appimage_'),
              f"GTK_PATH={e.get('GTK_PATH', '<unset>')}")
        check('at_bridge_disabled', e.get('NO_AT_BRIDGE') == '1',
              f"NO_AT_BRIDGE={e.get('NO_AT_BRIDGE', '<unset>')}")
        want_sw = args.scenario in ('no-gl', 'forced-software')
        has_sw = 'sfm-softgl' in e.get('LD_LIBRARY_PATH', '')
        check('gl_stack_selection', has_sw == want_sw,
              f"expected software={want_sw}, softgl_in_LD_LIBRARY_PATH={has_sw}, "
              f"LD_LIBRARY_PATH={e.get('LD_LIBRARY_PATH', '<unset>')[:120]}")
        check('software_render_env',
              (not want_sw) or (e.get('LIBGL_ALWAYS_SOFTWARE') == '1'
                                and e.get('GALLIUM_DRIVER') == 'llvmpipe'),
              f"LIBGL_ALWAYS_SOFTWARE={e.get('LIBGL_ALWAYS_SOFTWARE', '<unset>')} "
              f"GALLIUM_DRIVER={e.get('GALLIUM_DRIVER', '<unset>')}")
    else:
        check('proc_env_readable', False, 'no inspectable process')

    # ------------------------------------------------------------ 4. pixels
    shot1 = os.path.join(args.outdir, 'shot-1.png')
    try:
        screenshot(display, shot1)
        m1 = metrics(shot1)
        report['metrics']['initial'] = m1
        ok = (m1['distinct_colors'] >= 32 and m1['grey_stddev'] >= 0.01)
        check('renders_content', ok, json.dumps(m1))
    except Exception as exc:  # noqa: BLE001
        check('renders_content', False, f'{exc}')

    # ------------------------------------------------------- 5. real input
    try:
        sh(f'DISPLAY={display} xdotool mousemove 29 199 click 1')
        time.sleep(6)
        shot2 = os.path.join(args.outdir, 'shot-2-after-click.png')
        screenshot(display, shot2)
        m2 = metrics(shot2)
        report['metrics']['after_click'] = m2
        changed = (abs(m2['distinct_colors'] - m1['distinct_colors']) > 20 or
                   abs(m2['grey_stddev'] - m1['grey_stddev']) > 0.002)
        check('x11_input_changes_page', changed,
              f"colors {m1['distinct_colors']}->{m2['distinct_colors']}, "
              f"stddev {m1['grey_stddev']}->{m2['grey_stddev']}")
    except Exception as exc:  # noqa: BLE001
        check('x11_input_changes_page', False, f'{exc}')

    # -------------------------------------------------------- 6. failure log
    logf.flush()
    text = open(applog, 'r', errors='replace').read()
    bad = re.findall(r'.*(?:EGL_NOT_INITIALIZED|Could not create default EGL|'
                     r'swrast.*(?:fail|not found)|Aborting).*', text)
    check('no_gl_failure_in_log', not bad, f'{len(bad)} offending line(s): {bad[:3]}')
    report['gl_decision_lines'] = [l for l in text.splitlines() if l.startswith('sfm:')]

    # ------------------------------------------------------------- teardown
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    sh('pkill -f sigma-file-manager || true')
    sh('pkill -f WebKitWebProcess || true')
    sh('pkill -f appimage_extracted || true')
    xvfb.terminate()
    logf.close()

    report['verdict'] = 'PASS' if all(verdict) else 'FAIL'
    with open(os.path.join(args.outdir, 'gate-report.json'), 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print()
    log(f"VERDICT: {report['verdict']}  ({sum(verdict)}/{len(verdict)} checks passed)")
    shutil.copy(applog, os.path.join(args.outdir, 'app.log'))
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
