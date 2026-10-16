# Modified build — Chinese locale fix

This repository is a **fork** of
[aleksey-hoffman/sigma-file-manager](https://github.com/aleksey-hoffman/sigma-file-manager)
(upstream v2.2.0, 2026-07-17).

## What was changed

Only the Simplified Chinese locale file:

- `src/localization/messages/ch.json` — rewritten (325 of 1066 entries)
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

Built locally by the fork owner, **not** by the upstream project's CI:

- Ubuntu 24.04, WebKitGTK **2.52.6**, `NO_STRIP=true tauri build`
- Upstream's CI pins WebKitGTK to **2.44.0-2** to avoid
  `Could not create default EGL display: EGL_BAD_PARAMETER` on some drivers.
  2.44.0-2 is no longer available in the Ubuntu 24.04 archive, so this build
  uses the current system version. If you hit rendering glitches, run it with
  `WEBKIT_DISABLE_COMPOSITING_MODE=1 ./Sigma-File-Manager-*.AppImage`
  (the workaround documented in upstream's `CONTRIBUTING.md`).

## License

GNU GPLv3 or later, unchanged from upstream. See `LICENSE.md`.
This is an unofficial fork build, not affiliated with or endorsed by the
upstream project. The corresponding source for this binary is this
repository at the tag it was released from.
