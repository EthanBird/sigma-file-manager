# AppImage 硬化与 ABI 下限

本文记录把一份 UOS Desktop 20 打包踩坑记录（DRPA Next 的 `UOS20_PACKAGING.md`，
私有 glibc 运行层 / 固定安装根 / 递归 ELF 闭包 / 软件渲染闭包 / GTK 输入隔离 /
容器可视门禁）应用到 **sigma-file-manager 的 Linux AppImage** 上时，
哪些经验可以直接搬、哪些在 AppImage 这种打包格式里根本表达不出来，以及怎么复现。

结论先说：**其中一半经验可以直接搬，另一半与 AppImage 的单文件、随机挂载点特性
根本不相容——那一半正是 DRPA 最终选择固定根 deb 的原因。**

---

## 1. 先测量，再动手

在改任何东西之前，先测出当前产物的真实 ABI 需求和闭包缺口，而不是照着另一份
文档照抄结论。工具：`tools/linux/elf-audit.py` 与 `elf-audit-deep.py`。

对 v2.2.1-zh.1（Ubuntu 24.04 / glibc 2.39 构建）AppImage 的实测：

| 项目 | 实测值 |
| --- | --- |
| ELF 总数 | 197 |
| 带 `PT_INTERP` 的可执行文件 | 5（`AppRun.wrapped`、主程序、`WebKitNetworkProcess`、`WebKitWebProcess`、`libcap.so.2`） |
| `PT_INTERP` 值 | 全部为 `/lib64/ld-linux-x86-64.so.2`（**宿主**加载器） |
| 最高 `GLIBC_` 需求 | **2.39**（`usr/bin/sigma-file-manager`） |
| `libwebkit2gtk-4.1.so.0` 需求 | `GLIBC_2.38`、`GLIBCXX_3.4.30` |
| 动态搜索路径 | 195 个 ELF 用 `DT_RUNPATH=$ORIGIN`，1 个 `$ORIGIN/../lib` |
| 自带库 | 168 个（GTK 3、WebKitGTK 4.1、JavaScriptCoreGTK、GStreamer、Soup、ayatana-indicator…） |
| 自带 NSS/NSPR | **零个** |
| 自带 GL/EGL/GBM/libdrm/Mesa | **零个** |

三条直接可用的判断：

1. **ABI 下限 2.39**，意味着这份 AppImage 只能跑在 Ubuntu 24.04+ / Debian 13+，
   Ubuntu 22.04、Debian 12、以及所有基于 glibc 2.28～2.35 的国产桌面发行版全部出局。
   这是本次要修的第一件事。
2. **NSS 那组经验不适用。** DRPA 需要显式携带 `libsoftokn3` / `libfreebl3` /
   `libnssckbi` 和配套 `.chk`，是因为它们触发了 NSS 的 `dlopen` 路径。
   这个应用**根本没有链接 NSS**（WebKitGTK 在 Linux 上走 GnuTLS/GIO）。
   实测自带库中 NSS/NSPR 相关文件数为 0 —— 不存在这个坑，不用照搬。
3. **GL 栈完全依赖宿主。** 这是 DRPA 经验里真正可搬、且对本项目有意义的一条。

另外核对了 linuxdeploy 生成的 `apprun-hooks/linuxdeploy-plugin-gtk.sh` 所导出的
每一个路径在 AppDir 内是否真的存在（`GTK_PATH`、`GTK_IM_MODULE_FILE`、
`GDK_PIXBUF_MODULE_FILE`、`GIO_EXTRA_MODULES`、`GSETTINGS_SCHEMA_DIR`、
`GI_TYPELIB_PATH`）——**全部存在**，没有路径错配。这一项没有可修的东西。

同时确认了它**没有**做的事：不清除会话注入的 `GTK_MODULES` / `GTK3_MODULES`，
不设 `NO_AT_BRIDGE`，也不处理软件渲染 —— 这正是 DRPA 记录的输入冻结点。

---

## 2. 可以直接搬的经验

### 2.1 构建基线决定 ABI 下限（最大的一条）

Rust/Tauri 宿主、WebKitGTK、C++ 组件、GLVND/Mesa 都会把**构建机的 glibc 版本**
写成符号版本需求。在 Ubuntu 24.04 上构建，产物就要求 glibc 2.39，换发行版没用。
把它降到 Ubuntu 22.04（glibc 2.35）就能覆盖 Ubuntu 22.04/24.04、Debian 12、
以及 glibc ≥ 2.35 的一切系统。

22.04 也是 **webkit2gtk-4.1 存在的最老基线**：20.04 只有 `libwebkit2gtk-4.0`，
而 Tauri v2 / wry 用的是 4.1。所以 glibc 2.35 是这条路线的硬地板，不是随手选的。

> 构建环境见 `tools/linux/Dockerfile.build`。构建脚本会断言
> `getconf GNU_LIBC_VERSION` 精确等于 `glibc 2.35`。

### 2.2 GTK 模块隔离

应用自带 GTK 3/GLib，而目标桌面（尤其 DDE）会往环境里塞
`GTK_MODULES=gail:atk-bridge`、`GTK3_MODULES=atk-bridge`、自己的 `GTK_PATH`，
并通过 AT-SPI2 起辅助功能桥。把宿主的 GTK 模块加载进自带 GTK，等于在一个进程里
混两代 GTK/GLib；DRPA 观察到的表现是窗口还能被窗口管理器拖动，但页面里所有控件、
输入框和 Tauri IPC 入口全部停滞。

`tools/linux/appimage-apprun-hook.sh` 现在：

```sh
export GTK_PATH="$APPDIR/usr/lib/gtk-3.0"
export GTK_EXE_PREFIX="$APPDIR/usr"
unset GTK_MODULES GTK3_MODULES          # SFM_KEEP_GTK_MODULES=1 可保留
export NO_AT_BRIDGE=1                   # SFM_KEEP_AT_BRIDGE=1 可保留
```

**没有默认打开完整隔离**（`GTK_IM_MODULE=gtk-im-context-simple` +
`GDK_CORE_DEVICE_EVENTS=1`）。DRPA 把它作为最终修复，代价是放弃原生输入法预编辑
（"复杂文字可通过剪贴板输入"）。对一个中文用户日常使用的通用 AppImage，
默认关掉输入法是更糟的默认值。因此它做成了显式开关 `SFM_ISOLATED_INPUT=1`，
留给老 DDE 会话或排查时使用。

### 2.3 隔离的软件渲染闭包

宿主完全没有可用 GL 驱动时（最小容器、锁死的企业镜像、无 DRI 节点的环境），
WebKit 以 `Could not create default EGL display: EGL_BAD_PARAMETER` 退出，
窗口永久白屏 —— 这正是上游 changelog 修过、以及 DRPA 在 Fantasy II-M 上撞到的问题。

AppImage 现在自带一份**固定版本、与宿主隔离**的 Mesa llvmpipe 闭包，放在
`usr/lib/sfm-softgl/`（不参与默认搜索路径，因此不会遮蔽宿主的硬件 GL）：

```
sfm-softgl/50_mesa.json          # EGL vendor manifest
sfm-softgl/libEGL.so.1 libGL.so.1 libGLX.so.0 libGLdispatch.so.0 libOpenGL.so.0
sfm-softgl/libgbm.so.1 libdrm.so.2 libEGL_mesa.so.0 libLLVM-*.so.1 ...
sfm-softgl/dri/swrast_dri.so
```

启动器按以下优先级决定用哪套：

```
SFM_GL=software            强制软件
SFM_GL=hardware            强制硬件（不做任何干预）
SFM_GL=auto（默认）
   有 /dev/dri                            -> 硬件
   宿主存在 swrast_dri.so/kms_swrast_dri.so -> 硬件 + 宿主 llvmpipe
   宿主存在 /usr/share/glvnd/egl_vendor.d/50_mesa.json -> 硬件
   以上都没有                              -> 切到自带闭包
```

切到自带闭包时设置（仅此路径，硬件路径不注入任何变量）：

```sh
LIBGL_ALWAYS_SOFTWARE=1
GALLIUM_DRIVER=llvmpipe
LIBGL_DRIVERS_PATH=$APPDIR/usr/lib/sfm-softgl/dri
__EGL_VENDOR_LIBRARY_FILENAMES=$APPDIR/usr/lib/sfm-softgl/50_mesa.json
LD_LIBRARY_PATH=$APPDIR/usr/lib/sfm-softgl:$APPDIR/usr/lib:...
WEBKIT_DISABLE_COMPOSITING_MODE=1
```

关于 `LD_LIBRARY_PATH`：DRPA 明确拒绝全局导出它，因为会把私有 glibc 传染给
`xdg-open` 等系统子进程。这里可以接受，原因有两条，且都是可验证的：
**闭包里没有 libc/libstdc++/libgcc**（收集脚本显式跳过），以及**只在软件回退路径上导出**——
宿主 GL 可用时一个变量都不设。

`SFM_DEBUG=1` 会把每次 GL 判定打印到 stderr，便于现场排查。

---

## 3. 搬不过来的经验，以及为什么

### 3.1 私有 glibc 运行层 + 固定解释器 —— AppImage 表达不出来

DRPA 的核心手法是 `patchelf --set-interpreter /opt/drpa-next-uos20/uos-runtime/ld-linux-x86-64.so.2`，
把每个 ELF 的动态入口钉到包内私有加载器。**这要求解释器路径是编译期就确定、
运行期必然存在的绝对路径。** AppImage 挂载在 `/tmp/.mount_<随机后缀>`，
`--appimage-extract-and-run` 落在 `/tmp/appimage_extracted_<随机>`，每次都不同。
内核解析 `PT_INTERP` 时不展开 `$ORIGIN`，也不接受相对路径，所以：

- 无法把私有加载器写进 `PT_INTERP`；
- 退而求其次，让 `AppRun` 显式调用私有加载器
  （`$APPDIR/usr/lib/ld-linux-x86-64.so.2 --library-path ... AppRun.wrapped`）
  只能解决**主进程**：`WebKitNetworkProcess` / `WebKitWebProcess` 是 WebKit 自己
  `exec` 出来的，它们的 `PT_INTERP` 仍然是宿主的 `/lib64/ld-linux-x86-64.so.2`。
  实测这 4 个可执行文件的 `PT_INTERP` 全部指向系统加载器。

结论：**glibc 2.28 的 UOS 20 无法用单文件 AppImage 支持**，需要固定安装根下的
私有运行层 deb —— 也就是 DRPA 最终选择的那条路。这不是实现难度问题，是格式不兼容。

### 3.2 `cd $APPDIR/usr` 修 WebKit helper 路径 —— 本应用不需要

DRPA 遇到重定位后 `PKGLIBEXECDIR` 变成相对 `usr/` 的
`./lib/.../WebKitNetworkProcess`，于是让启动器先 `cd "$APPDIR/usr"`。
本应用实测**不需要**：在 `cwd` 完全无关的目录（`/tmp/zh-release`、容器内的
`/work`）启动，`WebKitWebProcess` / `WebKitNetworkProcess` 都能正常起来并就绪，
说明 helper 解析不依赖 `cwd`（linuxdeploy 重定位后走的是库自身目录）。
既然不需要，就没有照抄 —— 凭空 `cd` 会顺手改掉应用启动时的当前目录，
而"在终端中打开当前目录"这类功能是读它的。

### 3.3 `DT_RPATH` 强制传递 —— 本应用不需要

DRPA 必须 `patchelf --force-rpath` 生成传递型 `DT_RPATH`，并让验证器拒绝 `RUNPATH`。
原因是他们的私有 libc/C++ 与用户态闭包分层放在多个目录，`RUNPATH` 不传递会让子依赖找不到。

本 AppImage 实测 **195/197 个 ELF 的搜索路径是 `DT_RUNPATH=$ORIGIN`**，
且几乎所有库平铺在同一个 `usr/lib/` 下 —— 每个库在自己的目录里就能解析全部依赖，
不需要传递性。硬改成 `DT_RPATH` 只会改变解析顺序、增加与宿主库撞车的风险。
因此保留 `RUNPATH`，只把"交付前必须核对 RPATH/RUNPATH 完整"这一条纪律留下。

### 3.4 递归 `DT_NEEDED` 闭包 —— 部分适用

"只复制直接依赖、漏掉传递闭包"这条教训在这里表现为：
`libz`、`libfontconfig`、`libfreetype`、`libharfbuzz`、`libexpat`、`libxcb`、
`libstdc++`、`libgcc_s` 都不在包内，由宿主提供。
对 glibc ≥ 2.35 的现代发行版这是可接受的（它们必然有这些库），
也是 Tauri/linuxdeploy 的既定取舍。**在新的软件渲染闭包里我们反过来做**：
`softgl-closure.py` 从若干根出发递归解析 `DT_NEEDED`，把 llvmpipe 真正需要的一切
（含 `libLLVM`）都收进来，只跳过 C 运行时的六个名字。
门禁会验证切到软件路径后确实用的是包内闭包。

---

## 4. 门禁

`tools/linux/gate-appimage.py` 在 `ubuntu:22.04` 容器内运行，
该容器**不安装 `libwebkit2gtk-4.1-0`** —— 所以绿灯即证明自带运行时确实自足。
它检查的不是"进程活了 20 秒"，而是：

| # | 检查 |
| --- | --- |
| 1 | `getconf GNU_LIBC_VERSION` 精确为 `glibc 2.35`；无系统 WebKitGTK |
| 2 | 窗口被创建；`WebKitWebProcess` 在等待期后仍存活 |
| 3 | 从 `/proc/<pid>/environ` 读**活进程**环境：`GTK_MODULES`/`GTK3_MODULES` 已被清除、`GTK_PATH` 指在 AppDir 内、`NO_AT_BRIDGE=1` |
| 4 | 截图指标：颜色数 ≥ 32、灰度标准差 ≥ 0.01 |
| 5 | `xdotool` 投递**真实 X11 事件**点击侧栏后，页面必须发生变化 |
| 6 | GL 路径选择与场景一致（`no-gl` 场景必须走自带闭包并设置 `LIBGL_ALWAYS_SOFTWARE`/`GALLIUM_DRIVER`） |
| 7 | 日志中不得出现 `EGL_NOT_INITIALIZED`、`Could not create default EGL`、`Aborting` |

启动前主动向环境注入 DDE 风格的污染
（`GTK_MODULES=gail:atk-bridge`、`GTK3_MODULES=atk-bridge`、系统 `GTK_PATH`），
用以验证启动器确实清理掉了它们 —— 与 DRPA 门禁第 13 项的意图一致。

三个场景：`host-gl`（默认路径）、`no-gl`（把系统 Mesa DRI 目录移走，
验证自动回退）、`forced-software`（`SFM_GL=software`）。

---

## 5. 复现

```bash
# 1) 22.04 构建环境
docker build -f tools/linux/Dockerfile.build -t sfm-uos-build:22.04 tools/linux

# 2) 在容器里构建 AppImage，并生成软件渲染闭包
docker run --rm -v "$PWD":/src:ro -v "$PWD/tools/linux":/tools:ro -v /tmp/out:/out \
    sfm-uos-build:22.04 bash /tools/in-container-build.sh
#    （in-container-build.sh 见本文档同目录）

# 3) 静态 ABI 审计：确认 glibc 上限、RUNPATH、闭包缺口
python3 tools/linux/elf-audit.py       /tmp/extract/squashfs-root
python3 tools/linux/elf-audit-deep.py  /tmp/extract/squashfs-root

# 4) 注入启动器钩子与软件渲染闭包并重新打包
bash tools/linux/repack-appimage.sh /tmp/extract/squashfs-root \
     /tmp/out/sfm-softgl.tar.gz /tmp/out/Sigma-File-Manager.AppImage

# 5) 门禁（三个场景都要过）
docker run --rm -v /tmp/out:/out sfm-uos-build:22.04 \
    python3 /tools/gate-appimage.py --appimage /out/Sigma-File-Manager.AppImage \
    --outdir /out/gate-host-gl --scenario host-gl
```

---

## 6. 适用边界（必须如实声明）

- 本 AppImage 的 ABI 下限是 **glibc 2.35**（由构建基线决定）。
- **UOS Desktop 20 / Deepin 20.8（glibc 2.28）不在支持范围内。** 见 §3.1：
  要覆盖它们必须走固定安装根的 deb + 私有 glibc 运行层，而不是 AppImage。
- 容器门禁证明的是"在 glibc 2.35 用户态、没有系统 WebKitGTK、没有 GPU 的环境下
  能安装、能渲染、能响应真实输入"。它**不能**替代在真实 UOS 20 / kernel 4.19 /
  DDE / 实际显卡上的人工回归。
- 每次升级 Rust、Tauri、WebKitGTK、GTK、GStreamer、Mesa 闭包或构建基线，
  都必须重跑静态审计与三场景门禁。
