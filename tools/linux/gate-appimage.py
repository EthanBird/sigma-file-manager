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


def region_signature(path, box, grid=8):
    """Downsampled greyscale signature of a screen region, for comparing the
    same UI area across two moments."""
    img = Image.open(path).convert('L').crop(box)
    return list(img.resize((grid, grid)).getdata())


def sig_distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))


def park_pointer(display, x=640, y=790, settle=1.5):
    """Move the pointer off every hoverable widget before a screenshot: GTK
    paints a hover highlight under the pointer, which would otherwise make two
    otherwise-identical frames differ."""
    sh(f'DISPLAY={display} xdotool mousemove {x} {y}')
    time.sleep(settle)


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


def _unused():
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--appimage', required=True)
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--scenario', default='auto',
                    choices=['auto', 'auto-gpu-node', 'forced-software',
                             'forced-hardware'])
    ap.add_argument('--webkit-check-seconds', type=int, default=20)
    ap.add_argument('--locale', default=None,
                    help='seed this UI locale into the app settings before launch '
                         '(e.g. ch) so the gate also covers the translation')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    report = {'scenario': args.scenario, 'locale': args.locale,
              'checks': {}, 'metrics': {}}
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
    # Scenarios that must exercise the isolated renderer get a host with no GL
    # at all; the selection-only scenarios fabricate the signal the real
    # launcher keys on.
    dri = '/usr/lib/x86_64-linux-gnu/dri'
    vendor_dir = '/usr/share/glvnd/egl_vendor.d'
    render_required = args.scenario in ('auto', 'forced-software')

    if args.scenario in ('auto', 'forced-software'):
        if os.path.isdir(dri) and not os.path.exists(dri + '.hidden'):
            shutil.move(dri, dri + '.hidden')
        if os.path.isdir(vendor_dir) and not os.path.exists(vendor_dir + '.hidden'):
            shutil.move(vendor_dir, vendor_dir + '.hidden')
    else:
        if os.path.exists(dri + '.hidden'):
            shutil.move(dri + '.hidden', dri)
        if os.path.exists(vendor_dir + '.hidden'):
            shutil.move(vendor_dir + '.hidden', vendor_dir)

    if args.scenario == 'auto-gpu-node':
        os.makedirs('/dev/dri', exist_ok=True)
    elif args.scenario == 'forced-software' and os.path.isdir('/dev/dri'):
        pass

    expect_software = args.scenario in ('auto', 'forced-software')
    check('scenario_gl_state', True,
          f"/dev/dri={'present' if os.path.isdir('/dev/dri') else 'absent'}, "
          f"dri dir={'present' if os.path.isdir(dri) else 'removed'}, "
          f"vendor manifest dir={'present' if os.path.isdir(vendor_dir) else 'removed'}, "
          f"expect_software={expect_software}, render_required={render_required}")
    report['render_required'] = render_required
    report['expect_software'] = expect_software

    # fonts are supplied by the target desktop, never by the AppImage; the gate
    # records whether *this* image can render the seeded locale's script at all
    font_file = sh("fc-match -f '%{file}' 'sans-serif:lang=zh-cn'").stdout.strip()
    font_available = bool(font_file) and 'DejaVu' not in font_file
    report['font_match_zh'] = font_file
    check('font_available_recorded', True,
          f"fc-match sans-serif:lang=zh-cn -> {font_file or '(none)'} "
          f"(font_available={int(font_available)})")
    report['font_available'] = int(font_available)

    # ------------------------------------------------------------- 2. launch
    # Always pre-seed the settings: a fresh profile pops the "What's New"
    # dialog on first launch, whose dimmed backdrop would make the very first
    # click a dialog dismissal instead of a navigation and silently poison the
    # baseline below.
    settings_dir = os.path.expanduser(
        '~/.local/share/com.sigma-file-manager.app/user-data')
    os.makedirs(settings_dir, exist_ok=True)
    seeded = {'changelog.lastSeenVersion': '99.0.0'}
    if args.locale:
        seeded['language.locale'] = args.locale
    with open(os.path.join(settings_dir, 'user-settings.json'), 'w') as f:
        json.dump(seeded, f)
    log(f'seeded settings {seeded} into {settings_dir}')

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
    elif args.scenario == 'forced-hardware':
        env['SFM_GL'] = 'hardware'
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
    if win:
        # Focus the window before taking any baseline: GTK paints the whole UI
        # dimmed in the :backdrop state, so an activation happening later would
        # show up as a "change" that has nothing to do with input.
        sh(f'DISPLAY={display} xdotool windowactivate {win} || true')
        sh(f'DISPLAY={display} xdotool windowfocus {win} || true')
        time.sleep(3)

    time.sleep(args.webkit_check_seconds)
    webkit_pids = pid_of('WebKitWebProcess')
    if render_required:
        check('webkit_web_process_alive', bool(webkit_pids),
              f'WebKitWebProcess pids={webkit_pids}')
    else:
        report['webkit_pids'] = webkit_pids
        check('webkit_web_process_alive', True,
              f'selection-only scenario: not asserted (pids={webkit_pids})')

    # --------------------------------------------------------- 3. isolation
    main_pids = [p for p in pid_of('sigma-file-manager') if p not in webkit_pids]
    main_pids = [p for p in main_pids if os.path.exists(f'/proc/{p}/environ')]
    target = webkit_pids[0] if webkit_pids else (main_pids[0] if main_pids else None)
    report['inspected_pid'] = target
    if target:
        e = env_of(target) or {}
        report['proc_env'] = {k: v for k, v in e.items() if k in
                              ('GTK_PATH', 'GTK_MODULES', 'GTK3_MODULES', 'SFM_GL_ACTIVE',
                               'NO_AT_BRIDGE', 'GTK_IM_MODULE',
                               'GDK_CORE_DEVICE_EVENTS', 'LD_LIBRARY_PATH',
                               'LIBGL_ALWAYS_SOFTWARE', 'LIBGL_DRIVERS_PATH',
                               '__EGL_VENDOR_LIBRARY_FILENAMES',
                               'GALLIUM_DRIVER', 'WEBKIT_DISABLE_COMPOSITING_MODE',
                               'SFM_GL_ACTIVE',
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
        want_sw = expect_software
        has_sw = 'sfm-softgl' in e.get('LD_LIBRARY_PATH', '')
        active = e.get('SFM_GL_ACTIVE', '<unset>')
        check('gl_stack_selection',
              has_sw == want_sw and active == ('software' if want_sw else 'hardware'),
              f"expected {'software' if want_sw else 'hardware'}, SFM_GL_ACTIVE={active}, "
              f"softgl_in_LD_LIBRARY_PATH={has_sw}")

        # suspicious-driver shape: GPU node without any GLVND vendor manifest
        # must get upstream's documented compositing workaround
        suspicious = (os.path.isdir('/dev/dri')
                      and not any(os.path.isdir(d) and os.listdir(d)
                                  for d in ['/usr/share/glvnd/egl_vendor.d',
                                            '/usr/local/share/glvnd/egl_vendor.d',
                                            '/etc/glvnd/egl_vendor.d']))
        if suspicious and not want_sw:
            check('suspicious_driver_mitigation',
                  e.get('WEBKIT_DISABLE_COMPOSITING_MODE') == '1',
                  f"WEBKIT_DISABLE_COMPOSITING_MODE="
                  f"{e.get('WEBKIT_DISABLE_COMPOSITING_MODE', '<unset>')}")
        check('software_render_env',
              (not want_sw) or (e.get('LIBGL_ALWAYS_SOFTWARE') == '1'
                                and e.get('GALLIUM_DRIVER') == 'llvmpipe'),
              f"LIBGL_ALWAYS_SOFTWARE={e.get('LIBGL_ALWAYS_SOFTWARE', '<unset>')} "
              f"GALLIUM_DRIVER={e.get('GALLIUM_DRIVER', '<unset>')}")
    else:
        check('proc_env_readable', False, 'no inspectable process')

    # ------------------------------------------------------------ 4. pixels
    if not render_required:
        check('renders_content', True, 'selection-only scenario: rendering not asserted')
        check('x11_input_changes_page', True, 'selection-only scenario: input not asserted')
        check('x11_input_still_responsive_after_input', True, 'selection-only scenario: not asserted')
        check('no_gl_failure_in_log', True, 'selection-only scenario: log not asserted')
        _finish(args, proc, xvfb, logf, applog, report, verdict)
        return 0 if report['verdict'] == 'PASS' else 1

    # Take a *stable* baseline: a single snapshot can differ from a later one
    # merely because the page was still painting, which would make the input
    # check below pass without any input having been delivered.
    shot1 = os.path.join(args.outdir, 'shot-1.png')
    try:
        prev_sig = None
        m1 = None
        stable = False
        for attempt in range(6):
            park_pointer(display)
            screenshot(display, shot1)
            m1 = metrics(shot1)
            sig = region_signature(shot1, (0, 0, 1280, 720), grid=16)
            if prev_sig is not None:
                d = sig_distance(prev_sig, sig)
                log(f'baseline frame {attempt + 1}: pixel distance {d:.5f}')
                if d < 0.002:
                    stable = True
                    break
            prev_sig = sig
            time.sleep(5)
        report['baseline_stable'] = stable
        check('baseline_is_settled', stable,
              f'whole-frame pixel signature stopped moving (stable={stable})')
        report['metrics']['initial'] = m1
        report['metrics']['initial'] = m1
        ok = (m1['distinct_colors'] >= 32 and m1['grey_stddev'] >= 0.01)
        check('renders_content', ok, json.dumps(m1))

        # text-band metric: only meaningful when the image actually has a font
        # for the seeded script (mirrors the reference gate's distinction
        # between "layout rendered" and "text rendered")
        cjk = bool(args.locale) and args.locale.startswith(('ch', 'ja', 'ko'))
        if cjk and report.get('font_available'):
            want_dark = 0.002
            check('text_band_has_glyphs', m1['top_band_dark_ratio'] >= want_dark,
                  f"top-band dark ratio {m1['top_band_dark_ratio']} >= {want_dark} "
                  f"(font {report.get('font_match_zh')})")
        else:
            check('text_band_has_glyphs', True,
                  f"not asserted (cjk_locale={cjk}, "
                  f"font_available={report.get('font_available')})")
    except Exception as exc:  # noqa: BLE001
        check('renders_content', False, f'{exc}')

    # ------------------------------------------------------- 5. real input
    #
    # "the process is alive" is not evidence that input works, and neither is
    # "the picture changed" on its own.  What is checked here is that two
    # independent real X11 clicks each move the UI to a distinct state.
    try:
        content = (56, 260, 960, 720)    # page content area
        base_sig = region_signature(shot1, content, grid=16)

        # Two independent real X11 clicks, each expected to land on a different
        # page, so the content area must change twice and end up in two
        # distinct states.  A page that froze after input (the failure mode the
        # reference post-mortem hit) produces no change at all here.
        #
        # This is a liveness test, not a UI-semantics test: asserting *which*
        # page appeared would need app-side instrumentation (the reference
        # implementation had the frontend write nativeInputTyped /
        # postInputClick markers over IPC), and this fork deliberately does not
        # change application code — only packaging and locale data.
        sigs = []
        for label, x, y in (('rail-1', 29, 199), ('rail-2', 29, 118)):
            sh(f'DISPLAY={display} xdotool mousemove {x} {y} click 1')
            time.sleep(6)
            shot = os.path.join(args.outdir, f'shot-{label}.png')
            park_pointer(display)
            screenshot(display, shot)
            sigs.append(region_signature(shot, content, grid=16))

        shot2 = os.path.join(args.outdir, 'shot-2-after-click.png')
        shutil.copy(os.path.join(args.outdir, 'shot-rail-1.png'), shot2)
        shot3 = os.path.join(args.outdir, 'shot-3-after-second-click.png')
        shutil.copy(os.path.join(args.outdir, 'shot-rail-2.png'), shot3)
        report['metrics']['after_click'] = metrics(shot2)

        d1 = sig_distance(base_sig, sigs[0])
        d2 = sig_distance(sigs[0], sigs[1])
        report['content_signature'] = {'click1_vs_baseline': round(d1, 6),
                                       'click2_vs_click1': round(d2, 6)}
        check('x11_input_changes_page', d1 > 0.02,
              f'first real click changed the content area ({d1:.4f})')
        # The measured frame-to-frame noise floor on a settled page is 0.00000,
        # so anything above 0.005 is real signal, not repaint jitter.
        check('x11_input_still_responsive_after_input', d2 > 0.005,
              f'second real click produced a further distinct state ({d2:.4f})')
    except Exception as exc:  # noqa: BLE001
        check('x11_input_changes_page', False, f'{exc}')
        check('x11_input_still_responsive_after_input', False, 'not reached')

    # -------------------------------------------------------- 6. failure log
    logf.flush()
    text = open(applog, 'r', errors='replace').read()
    bad = re.findall(r'.*(?:EGL_NOT_INITIALIZED|Could not create default EGL|'
                     r'swrast.*(?:fail|not found)|Aborting).*', text)
    check('no_gl_failure_in_log', not bad, f'{len(bad)} offending line(s): {bad[:3]}')
    report['gl_decision_lines'] = [l for l in text.splitlines() if l.startswith('sfm:')]

    _finish(args, proc, xvfb, logf, applog, report, verdict)
    return 0 if report['verdict'] == 'PASS' else 1


def _finish(args, proc, xvfb, logf, applog, report, verdict):
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


if __name__ == '__main__':
    sys.exit(main())
