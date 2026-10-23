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
  8. isolated_input_default_on      GTK_IM_MODULE=gtk-im-context-simple, GDK_CORE_DEVICE_EVENTS=1, XMODIFIERS unset
  9. gio_module_dir_confined        host libgvfsdbus.so is NOT mapped into main process
 10. webkit_zero_host_so            zero host /usr/lib or /lib .so mapped into WebKitWebProcess
 11. startup_global_search_indexed  fresh startup scan completes without .index.staging race
 12. renders_content                distinct_colors >= 32, grey_stddev >= 1.0
 13. x11_click_1_navigator          real X11 click at (28, 125) navigates to Navigator
 14. quick_search_input_works       clicking Quick Search & typing 'Desktop' filters without freeze
 15. x11_click_2_settings           real X11 click at (28, 205) navigates to Settings
 16. language_combobox_opens        clicking Language BaseCombobox opens list & focuses input without freeze
 17. theme_combobox_opens           clicking Appearance -> Theme BaseCombobox opens list without freeze
 18. global_search_input_works      Global Search input is enabled, accepts typing & returns results
 19. no_gl_or_loader_errors_in_log  no EGL / GLIBC / symbol lookup errors in stderr log
"""
import argparse
import base64
import json
import os
import re
import shutil
import signal
import socket
import struct
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
    if win_id:
        sh(f'DISPLAY={display} xdotool windowactivate {win_id} || true')
        sh(f'DISPLAY={display} xdotool windowfocus {win_id} || true')
        sh(f'DISPLAY={display} xdotool mousemove 640 700 click 1 || true')
        time.sleep(0.5)
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


class InspectorClient:
    def __init__(self, port=9222):
        self.s = socket.create_connection(('127.0.0.1', port), timeout=10)
        self.s.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            'GET /socket/1/1/WebPage HTTP/1.1\r\n'
            f'Host: 127.0.0.1:{port}\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n'
        )
        self.s.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.s.recv(4096)
        self.rem = buf.split(b'\r\n\r\n', 1)[1]
        self.seq = 1
        first = self._recv_frame()
        self.target_id = first['params']['targetInfo']['targetId']

    def _recv_bytes(self, n):
        while len(self.rem) < n:
            chunk = self.s.recv(4096)
            if not chunk:
                raise RuntimeError('WebSocket EOF')
            self.rem += chunk
        out, self.rem = self.rem[:n], self.rem[n:]
        return out

    def _send_frame(self, obj):
        payload = json.dumps(obj).encode('utf-8')
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack('!H', n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack('!Q', n))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.s.sendall(bytes(header) + masked)

    def _recv_frame(self):
        b1, b2 = self._recv_bytes(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._recv_bytes(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._recv_bytes(8))[0]
        data = self._recv_bytes(length)
        return json.loads(data.decode('utf-8', 'replace'))

    def eval(self, expr):
        self.seq += 1
        req_id = self.seq
        inner = json.dumps({
            'id': req_id,
            'method': 'Runtime.evaluate',
            'params': {'expression': f'JSON.stringify(({expr})())', 'returnByValue': True},
        })
        self._send_frame({
            'id': req_id,
            'method': 'Target.sendMessageToTarget',
            'params': {'targetId': self.target_id, 'message': inner},
        })
        while True:
            m = self._recv_frame()
            if m.get('method') == 'Target.dispatchMessageFromTarget':
                msg = json.loads(m['params']['message'])
                if msg.get('id') == req_id:
                    val = msg.get('result', {}).get('result', {}).get('value')
                    return json.loads(val) if val else None

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


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

    # Clean previous instances, unlock DDE lock screen if active, and wipe global-search index
    # to test fresh startup indexing without the auxiliary-window race condition.
    sh('pkill -9 -f sigma-file-manager 2>/dev/null || true')
    sh('pkill -9 -f WebKit 2>/dev/null || true')
    sh(f'DISPLAY={args.display} xdotool type "123" 2>/dev/null || true')
    sh(f'DISPLAY={args.display} xdotool key Return 2>/dev/null || true')
    time.sleep(2)

    app_data_dir = os.path.expanduser('~/.local/share/com.sigma-file-manager.app')
    gs_dir = os.path.join(app_data_dir, 'global-search')
    if os.path.exists(gs_dir):
        shutil.rmtree(gs_dir)

    settings_dir = os.path.join(app_data_dir, 'user-data')
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
    settings['performance.prelaunchQuickViewWindow'] = True
    settings['lastRoute'] = {'name': 'home', 'fullPageId': ''}
    with open(settings_path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

    # Inject full DDE/fcitx session pollution before launching to verify cleaning
    env = dict(os.environ)
    env['DISPLAY'] = args.display
    env['SFM_DEBUG'] = '1'
    env['GTK_MODULES'] = 'gail:atk-bridge'
    env['GTK3_MODULES'] = 'atk-bridge'
    env['GTK_IM_MODULE'] = 'fcitx'
    env['XMODIFIERS'] = '@im=fcitx'
    env['QT_IM_MODULE'] = 'fcitx'
    env['SDL_IM_MODULE'] = 'fcitx'
    env['CLUTTER_IM_MODULE'] = 'fcitx'
    env['LD_LIBRARY_PATH'] = '/bogus/should/be/cleared'
    env['WEBKIT_INSPECTOR_HTTP_SERVER'] = '127.0.0.1:9222'

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
        time.sleep(1)
        sh(f'DISPLAY={args.display} xdotool mousemove 640 400 click 1 || true')
        time.sleep(1)

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
        check(
            'isolated_input_default_on',
            menv.get('GTK_IM_MODULE') == 'gtk-im-context-simple'
            and menv.get('GDK_CORE_DEVICE_EVENTS') == '1'
            and 'XMODIFIERS' not in menv,
            f"GTK_IM_MODULE={menv.get('GTK_IM_MODULE')}, "
            f"GDK_CORE_DEVICE_EVENTS={menv.get('GDK_CORE_DEVICE_EVENTS')}, "
            f"XMODIFIERS={menv.get('XMODIFIERS', '<unset>')}",
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

    # 11. Verify fresh startup global-search scan succeeded without .index.staging race
    gs_status_path = os.path.join(gs_dir, 'status.json')
    gs_status = {}
    for _ in range(15):
        if os.path.isfile(gs_status_path):
            try:
                gs_status = json.load(open(gs_status_path, encoding='utf-8'))
                if gs_status.get('last_scan_outcome') == 'completed':
                    break
            except Exception:
                pass
        time.sleep(1)
    check(
        'startup_global_search_indexed',
        gs_status.get('last_scan_outcome') == 'completed'
        and gs_status.get('indexed_item_count', 0) > 0
        and gs_status.get('last_scan_error') is None,
        f"outcome={gs_status.get('last_scan_outcome')}, "
        f"reason={gs_status.get('last_scan_reason')}, "
        f"indexed_item_count={gs_status.get('indexed_item_count')}, "
        f"error={gs_status.get('last_scan_error')}",
    )

    # 12. Visual rendering check
    shot_home = os.path.join(args.outdir, 'shot-1-home.png')
    shot_nav = os.path.join(args.outdir, 'shot-2-navigator.png')
    shot_set = os.path.join(args.outdir, 'shot-3-settings.png')

    sh(f'DISPLAY={args.display} xdotool mousemove --window {win_id} 640 700')
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

    insp = InspectorClient(9222)
    inspector_eval = insp.eval

    xwin_out = sh(f'DISPLAY={args.display} xwininfo -id {win_id}').stdout
    mx = re.search(r'Absolute upper-left X:\s+(-?\d+)', xwin_out)
    my = re.search(r'Absolute upper-left Y:\s+(-?\d+)', xwin_out)
    win_x = int(mx.group(1)) if mx else 10
    win_y = int(my.group(1)) if my else 10

    def click_rect(rect):
        if not rect:
            return False
        cx = win_x + int(rect['x'] + rect['width'] / 2)
        cy = win_y + int(rect['y'] + rect['height'] / 2)
        sh(f'DISPLAY={args.display} xdotool mousemove {cx} {cy} click 1')
        return True

    # 13. Click 1: Navigator icon on left rail (x=win_x+18, y=win_y+115)
    sh(f'DISPLAY={args.display} xdotool mousemove {win_x + 18} {win_y + 115} click 1')
    time.sleep(3)
    sh(f'DISPLAY={args.display} xdotool mousemove 640 700')
    time.sleep(1)
    capture_screen(args.display, shot_nav, win_id)
    sig_nav = region_signature(shot_nav, content_box)
    d1 = sig_distance(sig_home, sig_nav)
    check('x11_click_1_navigator', d1 > 0.02, f'content signature delta={d1:.4f}')

    # 14. Test Quick Search button click + typing 'Desktop'
    qs_btn = inspector_eval("""() => {
      const b = document.querySelector('.file-browser-toolbar-filter__button');
      return b ? b.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(qs_btn)
    time.sleep(1.5)
    sh(f'DISPLAY={args.display} xdotool type "Desktop"')
    time.sleep(1.5)
    qs_res = inspector_eval("""() => {
      const inp = document.querySelector('.file-browser-toolbar-filter__input');
      const pop = document.querySelector('.file-browser-toolbar-filter__popover');
      return {
        open: !!pop,
        val: inp ? inp.value : null,
        activeTag: document.activeElement ? document.activeElement.tagName : null
      };
    }""") or {}
    check(
        'quick_search_input_works',
        qs_res.get('open') is True and qs_res.get('val') == 'Desktop',
        json.dumps(qs_res, ensure_ascii=False),
    )
    sh(f'DISPLAY={args.display} xdotool key Escape')
    time.sleep(1)

    # 15. Test Global Search button click (in Navigator toolbar), input enabled state, typing & search results
    gs_btn = inspector_eval("""() => {
      const b = document.querySelector('.global-search-toolbar-button__button');
      return b ? b.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(gs_btn)
    time.sleep(2)
    sh(f'DISPLAY={args.display} xdotool type "Desktop"')
    time.sleep(2.5)
    gs_res = inspector_eval("""() => {
      const inp = document.querySelector('.global-search-view__input');
      const groups = document.querySelectorAll('.global-search-view__drive-group');
      const header = document.querySelector('.global-search-view__results-header');
      return {
        disabled: inp ? inp.disabled : true,
        value: inp ? inp.value : null,
        driveGroups: groups.length,
        headerText: header ? header.innerText.trim() : null
      };
    }""") or {}
    check(
        'global_search_input_works',
        gs_res.get('disabled') is False
        and gs_res.get('value') == 'Desktop'
        and gs_res.get('driveGroups', 0) > 0,
        json.dumps(gs_res, ensure_ascii=False),
    )
    gs_close = inspector_eval("""() => {
      const b = document.querySelector('.global-search-view__close');
      return b ? b.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(gs_close)
    time.sleep(1)

    # 16. Click 2: Settings icon on left rail (x=win_x+18, y=win_y+195)
    sh(f'DISPLAY={args.display} xdotool mousemove {win_x + 18} {win_y + 195} click 1')
    time.sleep(3)
    sh(f'DISPLAY={args.display} xdotool mousemove 640 700')
    time.sleep(1)
    capture_screen(args.display, shot_set, win_id)
    sig_set = region_signature(shot_set, content_box)
    d2 = sig_distance(sig_nav, sig_set)
    check('x11_click_2_settings', d2 > 0.01, f'content signature delta={d2:.4f}')

    # 17. Click '常规' (General) tab and test Language BaseCombobox
    gen_tab = inspector_eval("""() => {
      const tabs = [...document.querySelectorAll('.settings-page button')];
      const t = tabs.find(el => el.innerText.trim() === '常规' || el.innerText.trim() === 'General');
      return t ? t.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(gen_tab)
    time.sleep(1.5)
    lang_btn = inspector_eval("""() => {
      const b = document.querySelector('.combobox__trigger');
      return b ? b.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(lang_btn)
    time.sleep(1.5)
    lang_res = inspector_eval("""() => {
      const list = document.querySelector('.sigma-ui-combobox-list');
      const items = [...document.querySelectorAll('[role="option"]')].map(e => e.innerText.trim());
      return {
        open: !!list && list.getAttribute('data-state') === 'open',
        optionCount: items.length,
        activeTag: document.activeElement ? document.activeElement.tagName : null
      };
    }""") or {}
    check(
        'language_combobox_opens',
        lang_res.get('open') is True and lang_res.get('optionCount', 0) >= 5,
        json.dumps(lang_res, ensure_ascii=False),
    )
    click_rect(lang_btn)
    time.sleep(1)

    # 18. Click '界面外观' (Appearance) tab and test Theme BaseCombobox
    app_tab = inspector_eval("""() => {
      const tabs = [...document.querySelectorAll('.settings-page button')];
      const t = tabs.find(el => el.innerText.includes('界面外观') || el.innerText.includes('Appearance'));
      return t ? t.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(app_tab)
    time.sleep(1.5)
    theme_btn = inspector_eval("""() => {
      const b = document.querySelector('.combobox__trigger');
      return b ? b.getBoundingClientRect().toJSON() : null;
    }""")
    click_rect(theme_btn)
    time.sleep(1.5)
    theme_res = inspector_eval("""() => {
      const list = document.querySelector('.sigma-ui-combobox-list');
      const items = [...document.querySelectorAll('[role="option"]')].map(e => e.innerText.trim());
      return {
        open: !!list && list.getAttribute('data-state') === 'open',
        options: items
      };
    }""") or {}
    check(
        'theme_combobox_opens',
        theme_res.get('open') is True and len(theme_res.get('options', [])) >= 3,
        json.dumps(theme_res, ensure_ascii=False),
    )
    insp.close()

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
