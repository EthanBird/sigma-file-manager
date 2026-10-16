# Modified build — Chinese locale fix

This repository is a **fork** of
[aleksey-hoffman/sigma-file-manager](https://github.com/aleksey-hoffman/sigma-file-manager)
(upstream v2.2.0, 2026-07-17).

## What was changed

Only the Simplified Chinese locale file:

- `src/localization/messages/ch.json` — rewritten (325 of 1066 entries)
- `tools/linux/` — reproducible build, static ELF audit and headless gate
- `docs/APPIMAGE-HARDENING.md` — packaging rationale
- `package.json`, `src-tauri/Cargo.toml`,
  `src-tauri/default-file-manager-common/Cargo.toml`,
  `src-tauri/default-file-manager-launcher/Cargo.toml` — version bumped to
  `2.2.1-zh.1` so the fork build is distinguishable from the upstream release

No application logic, no other locale, no upstream source file was touched.
`git diff main...HEAD --stat` shows the full extent of the change relative to
the upstream default branch.

## Why

The bundled `ch.json` was a literal machine translation. It contained
semantic errors that changed the meaning of the UI (for example `Home` →
"首页" / home page for the home *directory*, `Documents` → "文件" / "files",
`Background manager` → "后台管理器" / *backend* manager, `To copy` → "复印" /
"photocopy", and the drive-card string `free of` → "自由于", which rendered as
"678 GB 自由于 1006 GB"). Terminology was also internally inconsistent
(`标签` / `选项卡` for tabs, `回收站` / `垃圾箱` / `垃圾桶` for trash,
`扩展` / `扩展功能` / `拓展功能` / `扩展程序` for extensions, mixed 你/您),
and 34 strings were left untranslated in English.

The rewrite follows the terminology the project itself already uses in its
Chinese changelog (`public/changelog/ch/changelog.md`), fixes the semantic
errors, unifies terminology, translates the remaining English strings, and
adds Chinese tokens to the settings-search tag strings so settings can be
found by searching in Chinese.

## Build provenance

Built locally by the fork owner, **not** by the upstream project's CI.

- Build base: **Ubuntu 22.04 (glibc 2.35)** — deliberately, because the build
  base sets the ABI floor of every binary in the bundle. The previous build
  used Ubuntu 24.04 and therefore required glibc 2.39, which locked the
  AppImage out of Ubuntu 22.04, Debian 12 and every current domestic
  enterprise desktop.
- Upstream's CI pins WebKitGTK to `2.44.0-2` because newer versions triggered
  `Could not create default EGL display: EGL_BAD_PARAMETER` on some drivers.
  That version is no longer in the Ubuntu archives; this build uses the
  current 22.04 package (WebKitGTK 2.50.4).
- The AppImage ships an isolated Mesa llvmpipe closure
  (`usr/lib/sfm-softgl/`) that the launcher falls back to automatically when
  the host exposes no usable GL driver at all. `SFM_GL=software` forces it,
  `SFM_GL=hardware` disables the fallback, `SFM_DEBUG=1` explains the choice.
- The launcher clears session-injected `GTK_MODULES` / `GTK3_MODULES` and sets
  `NO_AT_BRIDGE=1` so that host GTK modules are never loaded into the bundled
  GTK. `SFM_KEEP_GTK_MODULES=1` / `SFM_KEEP_AT_BRIDGE=1` opt out.
- Full rationale, the measured ABI audit, what was deliberately *not* copied
  from the reference post-mortem, and the verification gates:
  [`docs/APPIMAGE-HARDENING.md`](docs/APPIMAGE-HARDENING.md).

### Supported range

**glibc 2.35 or newer.** UOS Desktop 20 / Deepin 20.8 (glibc 2.28) are *not*
supported by this AppImage and cannot be, because a bundled private loader
requires a fixed install root — see §3.1 of the hardening document.

## License

GNU GPLv3 or later, unchanged from upstream. See `LICENSE.md`.
This is an unofficial fork build, not affiliated with or endorsed by the
upstream project. The corresponding source for this binary is this
repository at the tag it was released from.
