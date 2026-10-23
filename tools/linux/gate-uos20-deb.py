#!/usr/bin/env python3
"""Live acceptance gate for the UOS Desktop 20 (.deb) package.

Runs on a real UOS Desktop 20 (1070) / Deepin 20.8 host (glibc 2.28, kernel 4.19,
DDE X11 session) after `dpkg -i Sigma-File-Manager-*-uos20-amd64.deb`:

  1. host_abi_is_glibc_2_28         getconf GNU_LIBC_VERSION == 'glibc 2.28'
  2. deb_installed                  dpkg-query status is 'install ok installed'
  3. runtime_manifest_present       /opt/sigma-file-manager-uos20/uos-runtime-manifest.json
  4. window_created                 main 1280x720 X11 window appears on DISPLAY=:0
  5. webkit_processes_alive         WebKitNetworkProcess & WebKitWebProcess survive
  6. ld_library_path_unset          LD_LIBRARY_PATH is NOT in /proc/<pid>/environ
  7. gtk_modules_cleared            GTK_MODULES / GTK3_MODULES cleared, NO_AT_BRIDGE=1
  8. gio_module_dir_confined        host libgvfsdbus.so is NOT mapped into main process
  9. webkit_zero_host_so            zero host /usr/lib or /lib .so mapped into WebKitWebProcess
 10. renders_content                distinct_colors >= 32, grey_stddev >= 1.0
 11. x11_click_1_navigator          real X11 click at (28, 125) navigates to Navigator
 12. x11_click_2_settings           second real X11 click at (28, 205) navigates to Settings
 13. no_gl_or_loader_errors_in_log  no EGL / GLIBC / symbol lookup errors in stderr log
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

from PIL import Image, ImageStat


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


def capture_screen(display, out_path, win_id=None):
    code = f"""
import gi
gi.require_version('Gdk', '3.0')
gi.require_version('GdkX11', '3.0')
from gi.repository import Gdk, GdkX11
disp = Gdk.Display.open({display!r})
if {bool(win_id)!r}:
    win = GdkX11.X11Window.foreign_new_for_display(disp, int({(win_id or '0')!r}, 16))
else:
    win = disp.get_default_screen().get_root_window()
w, h = win.get_width(), win.get_height()
pb = Gdk.pixbuf_get_from_window(win, 0, 0, w, h)
if pb is None:
    root = disp.get_default_screen().get_root_window()
    pb = Gdk.pixbuf_get_from_window(root, 0, 0, root.get_width(), root.get_height())
pb.savev({out_path!r}, 'png', [], [])
"""
    r = sh(['python3', '-c', code])
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f'failed to capture screen to {out_path}: {r.stderr[:200]}')
    return out_path


def metrics(path):
    img = Image.open(path).convert('RGB')
    w, h = img.size
    colors = img.getcolors(maxcolors=1 << 24)
    ncolors = len(colors) if colors else -1
    grey = img.convert('L')
    stddev = ImageStat.Stat(grey).stddev[0]
    return {
        'size': [w, h],
        'distinct_colors': ncolors,
        'grey_stddev': round(stddev, 4),
    }


def region_signature(path, box, grid=16):
    img = Image.open(path).convert('L').crop(box)
    return list(img.resize((grid, grid)).getdata())


def sig_distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def env_of(pid):
    raw = open(f'/proc/{pid}/environ', 'rb').read()
    out = {}
    for item in raw.split(b'\0'):
        if b'=' in item:
            k, v = item.split(b'=', 1)
            out[k.decode('utf-8', 'replace')] = v.decode('utf-8', 'replace')
    return out


def mapped_host_sos(pid):
    sos = set()
    with open(f'/proc/{pid}/maps', 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 6 and '.so' in parts[5] and parts[5].startswith('/'):
                path = parts[5]
                if not path.startswith('/opt/sigma-file-manager-uos20'):
                    sos.add(path)
    return sorted(sos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--display', default=':0')
    ap.add_argument('--locale', default='ch')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    report = {
        'target': 'UOS Desktop 20 Professional (1070) amd64',
        'kernel': sh('uname -r').stdout.strip(),
        'locale': args.locale,
        'checks': {},
        'metrics': {},
    }
    verdict = []

    def check(name, ok, detail):
        report['checks'][name] = {'ok': bool(ok), 'detail': detail}
        verdict.append(bool(ok))
        print(f'[uos20-gate] {"PASS" if ok else "FAIL"}  {name}: {detail}', flush=True)

    # 1. Host ABI
    glibc = sh('getconf GNU_LIBC_VERSION').stdout.strip()
    check('host_abi_is_glibc_2_28', glibc == 'glibc 2.28', glibc)

    # 2. Package installed
    pkg_status = sh("dpkg-query -W -f='${Status} ${Version}' sigma-file-manager").stdout.strip()
    check('deb_installed', pkg_status.startswith('install ok installed'), pkg_status)

    # 3. Runtime manifest
    manifest_path = '/opt/sigma-file-manager-uos20/uos-runtime-manifest.json'
    has_manifest = os.path.isfile(manifest_path)
    manifest = json.load(open(manifest_path, encoding='utf-8')) if has_manifest else {}
    check(
        'runtime_manifest_present',
        has_manifest and manifest.get('privateGlibcVersion') == '2.35',
        f"patchedElfCount={manifest.get('patchedElfCount')}, "
        f"runtimeObjects={len(manifest.get('runtimeObjects', []))}",
    )

    # Clean previous instances, unlock DDE lock screen if active, and pre-seed settings
    sh('killall -9 sigma-file-manager WebKitNetworkProcess WebKitWebProcess 2>/dev/null || true')
    sh(f'DISPLAY={args.display} xdotool type "123" 2>/dev/null || true')
    sh(f'DISPLAY={args.display} xdotool key Return 2>/dev/null || true')
    time.sleep(2)

    settings_dir = os.path.expanduser('~/.local/share/com.sigma-file-manager.app/user-data')
    os.makedirs(settings_dir, exist_ok=True)
    settings_path = os.path.join(settings_dir, 'user-settings.json')
    settings = {}
    if os.path.exists(settings_path):
        try:
            settings = json.load(open(settings_path, encoding='utf-8'))
        except Exception:
            settings = {}
    settings['changelog.lastSeenVersion'] = '99.0.0'
    settings['language.locale'] = args.locale
    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    # Inject DDE-style session pollution before launching to verify cleaning
    env = dict(os.environ)
    env['DISPLAY'] = args.display
    env['SFM_DEBUG'] = '1'
    env['GTK_MODULES'] = 'gail:atk-bridge'
    env['GTK3_MODULES'] = 'atk-bridge'
    env['LD_LIBRARY_PATH'] = '/bogus/should/be/cleared'

    applog = os.path.join(args.outdir, 'app.log')
    logf = open(applog, 'wb')
    proc = subprocess.Popen(
        ['/usr/bin/sigma-file-manager'],
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )

    win_id = ''
    for _ in range(30):
        time.sleep(1)
        out = sh(f'DISPLAY={args.display} xwininfo -root -tree').stdout
        for line in out.splitlines():
            if '1280x720' in line and '"Sigma File Manager"' in line:
                win_id = line.strip().split()[0]
                break
        if win_id:
            break

    check('window_created', bool(win_id), f'window_id={win_id or "none"}')
    time.sleep(14)

    if win_id:
        sh(f'DISPLAY={args.display} xdotool windowactivate {win_id} || true')
        sh(f'DISPLAY={args.display} xdotool windowfocus {win_id} || true')
        time.sleep(2)

    main_pids = [
        int(p)
        for p in sh("pgrep -f '/opt/sigma-file-manager-uos20/usr/bin/sigma-file-manager'").stdout.split()
        if p.isdigit()
    ]
    net_pids = [int(p) for p in sh("pgrep -f 'WebKitNetworkProcess'").stdout.split() if p.isdigit()]
    web_pids = [int(p) for p in sh("pgrep -f 'WebKitWebProcess'").stdout.split() if p.isdigit()]

    check(
        'webkit_processes_alive',
        bool(main_pids) and bool(net_pids) and bool(web_pids),
        f'main={main_pids}, net={net_pids}, web={web_pids}',
    )

    if main_pids and web_pids:
        mpid = main_pids[0]
        wpid = web_pids[0]
        menv = env_of(mpid)
        check(
            'ld_library_path_unset',
            'LD_LIBRARY_PATH' not in menv,
            f"LD_LIBRARY_PATH={menv.get('LD_LIBRARY_PATH', '<unset>')}",
        )
        check(
            'gtk_modules_cleared',
            'GTK_MODULES' not in menv
            and 'GTK3_MODULES' not in menv
            and menv.get('NO_AT_BRIDGE') == '1',
            f"GTK_MODULES={menv.get('GTK_MODULES', '<unset>')}, "
            f"NO_AT_BRIDGE={menv.get('NO_AT_BRIDGE', '<unset>')}",
        )
        main_host_sos = mapped_host_sos(mpid)
        gvfs_leaked = any('gvfs' in s or '/gio/modules/' in s for s in main_host_sos)
        check(
            'gio_module_dir_confined',
            not gvfs_leaked and menv.get('GIO_MODULE_DIR', '').startswith('/opt/sigma-file-manager-uos20'),
            f'host_sos={main_host_sos}',
        )
        web_host_sos = mapped_host_sos(wpid)
        check('webkit_zero_host_so', len(web_host_sos) == 0, f'web_host_sos={web_host_sos}')

    # Visual & real X11 input checks
    shot_home = os.path.join(args.outdir, 'shot-1-home.png')
    shot_nav = os.path.join(args.outdir, 'shot-2-navigator.png')
    shot_set = os.path.join(args.outdir, 'shot-3-settings.png')

    sh(f'DISPLAY={args.display} xdotool mousemove 640 700')
    time.sleep(1)
    capture_screen(args.display, shot_home, win_id)
    m_home = metrics(shot_home)
    report['metrics']['home'] = m_home
    check(
        'renders_content',
        m_home['distinct_colors'] >= 32 and m_home['grey_stddev'] >= 1.0,
        json.dumps(m_home),
    )

    content_box = (56, 50, 1240, 700)
    sig_home = region_signature(shot_home, content_box)

    # Click 1: Navigator icon on left rail (x=28, y=125)
    sh(f'DISPLAY={args.display} xdotool mousemove 28 125 click 1')
    time.sleep(4)
    sh(f'DISPLAY={args.display} xdotool mousemove 640 700')
    time.sleep(1)
    capture_screen(args.display, shot_nav, win_id)
    sig_nav = region_signature(shot_nav, content_box)
    d1 = sig_distance(sig_home, sig_nav)

    # Click 2: Settings icon on left rail (x=28, y=205)
    sh(f'DISPLAY={args.display} xdotool mousemove 28 205 click 1')
    time.sleep(4)
    sh(f'DISPLAY={args.display} xdotool mousemove 640 700')
    time.sleep(1)
    capture_screen(args.display, shot_set, win_id)
    sig_set = region_signature(shot_set, content_box)
    d2 = sig_distance(sig_nav, sig_set)

    report['content_signature'] = {
        'navigator_vs_home': round(d1, 6),
        'settings_vs_navigator': round(d2, 6),
    }
    check('x11_click_1_navigator', d1 > 0.02, f'content signature delta={d1:.4f}')
    check('x11_click_2_settings', d2 > 0.01, f'content signature delta={d2:.4f}')

    logf.flush()
    log_text = open(applog, 'r', encoding='utf-8', errors='replace').read()
    bad_lines = re.findall(
        r'.*(?:EGL_NOT_INITIALIZED|Could not create default EGL|'
        r'version `GLIBC_|symbol lookup error|Inconsistency detected by ld\.so|Aborting).*',
        log_text,
    )
    check('no_gl_or_loader_errors_in_log', not bad_lines, f'offending={bad_lines[:3]}')

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    logf.close()

    report['verdict'] = 'PASS' if all(verdict) else 'FAIL'
    report['passed'] = sum(verdict)
    report['total'] = len(verdict)
    report_path = os.path.join(args.outdir, 'gate-report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n[uos20-gate] VERDICT: {report['verdict']} ({report['passed']}/{report['total']} checks passed)")
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
