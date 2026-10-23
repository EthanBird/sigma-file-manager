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

## 6. 本次实测结果

| 项目 | `-zh.1`（Ubuntu 24.04 构建） | `-zh.2`（Ubuntu 22.04 构建） |
| --- | --- | --- |
| 主程序最高 `GLIBC_` 需求 | 2.39 | **2.35** |
| `libwebkit2gtk-4.1` 需求 | 2.38 | 2.35 |
| 文件系统内 ELF 数 | 197 | 235（含软渲染闭包） |
| 自带库 | 168 | 173 + 33（`sfm-softgl`） |
| AppImage 体积 | 92.4 MB | 142.3 MB |
| 可运行于 | Ubuntu 24.04+ / Debian 13+ | **Ubuntu 22.04+ / Debian 12+** |

新增的 50 MB 主要是 Mesa 的 llvmpipe 闭包（`swrast_dri.so` 32.6 MB + `libLLVM-15.so.1` 116.8 MB，
squashfs 压缩后约 42 MB）。它只在软件回退路径上生效。若要砍掉这段体积，
删掉 `usr/lib/sfm-softgl` 即可让启动器退回"只用宿主 GL"的行为——
代价是没有 GL 驱动的机器会白屏。

门禁四个场景全部通过：

```
auto               17/17   （无 GPU、无 EGL vendor manifest → 自动用自带软渲染）
forced-software    17/17
forced-hardware    15/15   （只断言选择逻辑）
auto-gpu-node      16/16   （假 /dev/dri → 选硬件 + 关 WebKit compositing）
```

`auto` 场景的关键读数：
`distinct_colors=17962`、`grey_stddev=25.77`、文本框深色像素比 `0.9535`；
第一次真实点击内容区签名移动 `0.0408`，第二次点击再移动 `0.0176`（基线噪声实测为 `0.00000`）。

---

## 7. 门禁抓到的真实缺陷

这一节是本流程最有价值的部分：下面每一条都是**门禁跑出来的**，不是推理出来的。
静态检查和"进程活着"都会漏掉它们。

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| `sigma-file-manager: error while loading shared libraries: libgbm.so.1` —— 连 `main()` 都到不了 | Tauri/linuxdeploy 把 libgbm/libdrm/libEGL 排除在包外，交给宿主；最小系统上不存在。`ldd` 在干净容器里恰好报两个：`libgbm.so.1`、`libEGL.so.1` | `promote-runtime-libs.py` 把加载期必需的 GL/GBM 库从闭包提升进 `usr/lib`（`RUNPATH=$ORIGIN` 可达）；厂商驱动 `libEGL_mesa` 仍只留在 `sfm-softgl`，硬件加速照旧走宿主的 vendor manifest |
| `Could not create default EGL display: EGL_BAD_PARAMETER. Aborting...`（宿主有 `swrast_dri.so` 却没有 vendor manifest） | 自动判定把"存在 DRI 驱动"当成"EGL 可用"。**DRI 驱动 ≠ EGL 可用**：GLVND 需要 vendor manifest 才能派发 | 判定改为只看 `/dev/dri` 与 GLVND vendor manifest，删掉了那条错误启发式 |
| `Couldn't open libGLESv2.so.2`（软渲染路径） | 闭包只收了 EGL/GL，漏了 WebKit 会直接 `dlopen` 的 GLES | 闭包根加入 `libGLESv2.so.2`、`libGLESv1_CM.so.1` |
| 容器里 `failed to run linuxdeploy` | Tauri bundler 在无 FUSE 容器内的调用方式问题——**同一条 linuxdeploy 命令手动执行却成功**（rc=0） | 不纠缠：`finish-bundle.sh` 用同一套 linuxdeploy + gtk 插件接手该步骤，产物等价 |
| `/usr/bin/xdg-open: No such file or directory` | 22.04 基础镜像没装 `xdg-utils`，而 Tauri 的 Linux bundler 必须把 `/usr/bin/xdg-open` 塞进包 | 构建镜像与构建脚本都安装 `xdg-utils` |
| 门禁"进程存活 20 秒"就通过 | 旧式门禁看不到 EGL 白屏，也看不到输入冻结 | 门禁改为读 `/proc/<pid>/environ`（活性）、像素指标、两次真实 X11 点击 |
| 门禁"截图变了就算输入生效" | 页面重绘、首次点击让窗口脱离 GTK `:backdrop`、基线未稳定，都会造成假阳性 | 基线用整屏像素签名判稳（实测噪声 `0.00000`）后才开始测量；指针先停到中性位再截图（GTK 悬停高亮会污染区域）；点击目标改为内容区并要求**两次独立点击各自产生不同状态** |
| 门禁容器里中文渲染成豆腐块 | 最小镜像没有 CJK 字体——与参考手册 §11 的 `font_available=0` 完全同源 | 门禁镜像装 `fonts-noto-cjk`，记录 `fc-match sans-serif:lang=zh-cn`，并在 CJK 场景下断言文本带深色像素比 |

其中前三条是**真实的、会导致不可用的缺陷**，全部由门禁发现。

---

## 8. 适用边界（必须如实声明）

- 本 AppImage 的 ABI 下限是 **glibc 2.35**（由构建基线决定）。
- 还要求 **`libstdc++` ≥ GCC 12**：实测最高需求为 `GLIBCXX_3.4.30`
  （来自 `libjavascriptcoregtk-4.1`），软渲染闭包里的 `libLLVM-15` 还要 `CXXABI_1.3.13`。
  Ubuntu 22.04 与 Debian 12 都满足；`glibc` 够新但 `libstdc++` 停留在 GCC 11
  （如 RHEL 9）的系统会在加载期失败。这正是参考手册强调 C++ ABI 时代的原因。
- **UOS Desktop 20 / Deepin 20.8（glibc 2.28）无法由单文件 AppImage 支持**（见 §3.1），
  但已由配套的**固定安装根 `.deb` + 私有 glibc 2.35 运行层**（见 §9）完整覆盖并在 UOS 20 真机通过门禁。
- 容器门禁证明的是"在 glibc 2.35 用户态、没有系统 WebKitGTK、没有 GPU 的环境下
  能安装、能渲染、能响应真实输入"。它**不能**替代在真实 UOS 20 / kernel 4.19 /
  DDE / 实际显卡上的人工回归。
- 每次升级 Rust、Tauri、WebKitGTK、GTK、GStreamer、Mesa 闭包或构建基线，
  都必须重跑静态审计与三场景门禁。

---

## 9. UOS Desktop 20（glibc 2.28）固定安装根 `.deb` 与真机门禁验证

为覆盖 **统信 UOS Desktop 20 Professional (1070) / Deepin 20.8（glibc 2.28、GLIBCXX_3.4.25、Linux 4.19）**，
本仓库新增 `tools/linux/build-uos20-deb.py` 与真机门禁 `tools/linux/gate-uos20-deb.py`，
将硬化后的 `v2.2.1-zh.2` AppImage 重打包为固定安装根 `/opt/sigma-file-manager-uos20` 的 Debian 软件包
`Sigma-File-Manager-2.2.1-zh.2-uos20-amd64.deb`（包版本 `2.2.1-zh.2+uos20.2`）。

### 9.1 固定安装根 `.deb` 架构设计

1. **私有 `glibc 2.35` + 传递闭包目录（`/opt/sigma-file-manager-uos20/uos-runtime`）**：
   - 从 Ubuntu 22.04（`glibc 2.35`）构建机收集 13 个 glibc 核心对象（`ld-linux-x86-64.so.2`、`libc.so.6`、`libm.so.6`、`libdl.so.2`、`libpthread.so.0`、`librt.so.1`、`libresolv.so.2`、`libutil.so.1`、`libanl.so.1`、`libBrokenLocale.so.1`、`libnss_files.so.2`、`libnss_dns.so.2`、`libnss_compat.so.2`）、C++ 运行时（`libstdc++.so.6`、`libgcc_s.so.1`），以及 AppDir 内未自带但由 `DT_NEEDED` 引用的 8 个用户态库（`libharfbuzz.so.0`、`libfontconfig.so.1`、`libfreetype.so.6`、`libfribidi.so.0`、`libgmp.so.10`、`libcom_err.so.2`、`libgpg-error.so.0`、`libgraphite2.so.3`），共 **23 个运行时对象**。
   - 构建期静态审计断言包内全部 ELF 的 `DT_NEEDED` 100% 在包内闭环解析（0 个外部缺口）。
2. **`patchelf >= 0.14` 改写固定 `PT_INTERP` 与传递型 `DT_RPATH`**：
   - 对 4 个带解释器的可执行文件（`AppRun.wrapped`、`usr/bin/sigma-file-manager`、`WebKitNetworkProcess`、`WebKitWebProcess`）将 `PT_INTERP` 设为 `/opt/sigma-file-manager-uos20/uos-runtime/ld-linux-x86-64.so.2`。
   - 对除 glibc 核心对象外的全部 **222 个 ELF** 执行 `patchelf --force-rpath --set-rpath /opt/sigma-file-manager-uos20/uos-runtime:/opt/sigma-file-manager-uos20/usr/lib:/opt/sigma-file-manager-uos20/usr/lib/sfm-softgl`，将非传递的 `DT_RUNPATH` 转为传递型 `DT_RPATH`，并在每个文件改写前后比对 `DT_NEEDED` 列表防止损坏。
3. **彻底零 `LD_LIBRARY_PATH`（避免宿主子进程崩溃）**：
   - 由于传递型 `DT_RPATH` 已覆盖主进程、WebKit 辅助进程及 `dlopen` 的 `swrast_dri.so`，启动器 `/usr/bin/sigma-file-manager` **显式 `unset LD_LIBRARY_PATH`**，并移除包内自带的 Ubuntu 22.04 `usr/bin/xdg-open` 脚本。
   - 这样应用在启动或运行期调用 `xdg-open`、`which`、`gsettings`、`kreadconfig5` 或打开系统终端时，宿主 `glibc 2.28` 子进程完全不会被私有 `glibc 2.35` 污染。
4. **默认开启完整 DDE 输入与 IM 隔离（解决点击 Quick Search / 语言切换 / 主题切换死锁）**：
   - 在 UOS 20 DDE 会话下，宿主环境默认注入 `GTK_IM_MODULE=fcitx`、`XMODIFIERS=@im=fcitx`、`QT_IM_MODULE=fcitx` 及 XInput2。普通目录点击与左侧导航栏切换不聚焦 `<input>` 因此看似正常；但**一旦点击 Quick Search、设置页语言切换（`BaseCombobox` 自动聚焦内部搜索 `<input>`）、主题切换（`BaseCombobox`）或全局搜索输入框**，包内 Ubuntu 22.04 的 GTK 3 / WebKitGTK 会立即激活 `GtkIMContext` 连接宿主 `fcitx`/搜狗输入法，跨两代 GTK/GLib 用户态导致 WebKitGTK 输入循环瞬间死锁（弹窗无法绘出、整窗按钮全部失灵）。
   - UOS 20 启动器因此**默认开启 `SFM_ISOLATED_INPUT=1`**（`unset XMODIFIERS QT_IM_MODULE SDL_IM_MODULE CLUTTER_IM_MODULE`、`export GTK_IM_MODULE=gtk-im-context-simple`、`export GDK_CORE_DEVICE_EVENTS=1`）；AppImage 启动钩子 `appimage-apprun-hook.sh` 也在检测到 `Deepin`/`UOS`/`fcitx` 会话时自动启用该隔离。
5. **修复 `quick-view` 预启动辅助窗口与全局搜索启动索引的竞态（解决全局搜索被禁用无法输入）**：
   - 应用默认开启 `performance.prelaunchQuickViewWindow: true`。主窗口启动时调用 `global_search_init` 并启动 `startup` 索引扫描（创建 `.index.staging.*` 目录写入 Tantivy 段文件，耗时约 3 秒）；1 秒后预启动的 `quick-view` 辅助窗口加载 `App.vue` 同样执行了未加 `isMainWindow` 守卫的 `useInit()` -> `global_search_init()`。
   - Rust 端 `global_search_init()` 原先未检查 `is_scan_in_progress` 就无条件执行 `cleanup_orphan_index_dirs(&base_dir)`，直接把主窗口正在写入的 `.index.staging.*` 删空并把 `scan_phase` 重置为 `Idle`，导致启动索引必败（`FileDoesNotExist(".../.index.staging.../....fieldnorm")`），`indexed_item_count` 保持为 `0`，进而触发 `global-search-view.vue` 的 `:disabled="!hasIndexData && globalSearchStore.scanPhase === 'idle'"` 将全局搜索输入框永久禁用并显示「搜索数据不完整 / 未选择驱动器」。
   - 修复：`src-tauri/src/global_search/scan.rs` 的 `global_search_init()` 在扫描进行中或索引已初始化时直接返回当前状态；`src/composables/use-init.ts` 将 `globalSearch.initOnLaunch` 等后台初始化限制在 `isMainWindow`；`global-search-view.vue` 移除输入框的 `:disabled` 锁死并在打开面板且无索引时自动触发扫描。
6. **`GIO_MODULE_DIR` 严格隔离宿主 GIO 模块 + 默认启用 `SFM_GL=software`**：
   - 显式设置 `GIO_MODULE_DIR="$APPDIR/usr/lib/x86_64-linux-gnu/gio/modules"` 阻断宿主 `libgvfsdbus.so` 混入；默认启用 `SFM_GL=software` 走包内 `usr/lib/sfm-softgl` 的 Mesa `llvmpipe`。

### 9.2 在 UOS Desktop 20 (1070) 真机抓到的 7 个真实问题

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 直接运行 `-zh.2` AppImage 报 `GLIBC_2.29`..`2.35` / `GLIBCXX_3.4.30` not found | AppImage 随机挂载点无法固定 `PT_INTERP`，`WebKitNetworkProcess`/`WebKitWebProcess` 由内核按 `/lib64/ld-linux-x86-64.so.2`（宿主 glibc 2.28）启动 | `build-uos20-deb.py` 采用固定根 `/opt/sigma-file-manager-uos20` + 私有 `glibc 2.35` 加载器 |
| 在 UOS 20 上用系统自带 `patchelf 0.10` 改写二进制后报 `libwebkit2gtk-4.1.so.0: cannot open shared object file` 或 `swrast_dri.so` 段错误 | `patchelf 0.10` 改写已被 `linuxdeploy` 处理过的 ELF 时存在已知 bug，会丢弃 `.dynamic` 中的 `RUNPATH` 和前两个 `DT_NEEDED`（`liblzma.so.5`、`libgio-2.0.so.0`）并损坏 TLS 表 | 构建脚本强制校验 `patchelf >= 0.14`（实测 `0.14.3`），并在每次 `patchelf` 前后断言 `DT_NEEDED` 完全一致 |
| `dmesg` 出现 `gsettings`、`which`、`kreadconfig5` 在 `libc.so.6` / `ld-2.28.so` 中段错误 | 若通过 `LD_LIBRARY_PATH` 暴露私有 `glibc 2.35` 目录，应用启动时调用 `xdg-open` 派生的宿主 `glibc 2.28` 工具链会加载 `glibc 2.35` 的 `libc.so.6` 崩溃 | 全量改用传递型 `DT_RPATH`，启动器 `unset LD_LIBRARY_PATH`，并删除包内 `usr/bin/xdg-open` 回退给宿主 |
| `/proc/<pid>/maps` 中混入宿主 `/usr/lib/x86_64-linux-gnu/gio/modules/libgvfsdbus.so` | `linuxdeploy-plugin-gtk.sh` 只设了追加型的 `GIO_EXTRA_MODULES`，未覆盖默认模块目录 | 启动器显式设置 `GIO_MODULE_DIR="$APPDIR/usr/lib/x86_64-linux-gnu/gio/modules"` |
| VMware UOS 20 虚拟机下 `SFM_GL=auto` 选 `hardware` 导致 `libEGL fatal: did not find extension DRI_Mesa version 1` | VMware 暴露了 `/dev/dri` 与宿主 `50_mesa.json`，但 UOS 20 宿主旧版 `vmwgfx_dri.so` 与包内新 `libgbm`/`libEGL` 不兼容 | UOS 20 专用启动器默认 `SFM_GL=software` 走包内 `sfm-softgl` |
| **点击 Quick Search、设置里语言切换、主题切换立即卡死**（打开目录和切换左栏菜单却正常） | `BaseCombobox`（语言/主题切换）与 Quick Search 打开时都会自动 `.focus()` 内部 `<input>`；UOS 20 DDE 会话注入的 `GTK_IM_MODULE=fcitx` / `XMODIFIERS=@im=fcitx` 在首次聚焦 `<input>` 时跨用户态激活输入法桥接，阻塞 WebKitGTK 输入循环 | UOS 20 启动器默认启用 `SFM_ISOLATED_INPUT=1`（`GTK_IM_MODULE=gtk-im-context-simple`、`GDK_CORE_DEVICE_EVENTS=1` 并清除 `XMODIFIERS`/`*_IM_MODULE`） |
| **全局搜索输入框呈禁用态（`disabled`）无法输入**，提示「搜索数据不完整 / 未选择驱动器」 | 预启动的 `quick-view` 辅助窗口同样执行了 `useInit()` -> `global_search_init()`，而 `global_search_init()` 无条件执行 `cleanup_orphan_index_dirs` 删掉了主窗口正在写入的 `.index.staging.*`，导致首次启动索引必败（`indexed_item_count=0`）并触发 `<Input :disabled="!hasIndexData && scanPhase === 'idle'">` | `global_search_init` 增加 `is_scan_in_progress || index.is_some()` 守卫、`use-init.ts` 限制仅 `isMainWindow` 初始化后台全局搜索、`global-search-view.vue` 移除输入框 `:disabled` 锁死 |

### 9.3 复现构建与真机门禁

```bash
# 1) 在 Ubuntu 22.04 (glibc 2.35, patchelf >= 0.14) 环境构建前端、Rust 主程序与 UOS 20 .deb
npm ci --ignore-scripts
npm run build-only
npx tauri build --no-bundle --config '{"build":{"beforeBuildCommand":""}}'
python3 tools/linux/build-uos20-deb.py \
    --appimage Sigma-File-Manager-2.2.1-zh.2-linux.AppImage \
    --replace-binary src-tauri/target/release/sigma-file-manager \
    --out Sigma-File-Manager-2.2.1-zh.2-uos20-amd64.deb \
    --pkg-version 2.2.1-zh.2+uos20.2

# 2) 在 UOS Desktop 20 (1070, glibc 2.28) 真机安装并运行 19 项门禁
sudo dpkg -i Sigma-File-Manager-2.2.1-zh.2-uos20-amd64.deb
python3 tools/linux/gate-uos20-deb.py --outdir /tmp/uos20-gate-out
```

UOS Desktop 20 Professional (1070, `4.19.0-amd64-desktop`, `glibc 2.28`) **19/19 项真机门禁全部通过**：
- `host_abi_is_glibc_2_28`: `glibc 2.28`
- `deb_installed`: `install ok installed 2.2.1-zh.2+uos20.2`
- `runtime_manifest_present`: `patchedElfCount=222, runtimeObjects=23`
- `window_created` & `webkit_processes_alive`: 主窗口（`1280x720`）、`WebKitNetworkProcess`、`WebKitWebProcess` 全部存活
- `ld_library_path_unset`: `LD_LIBRARY_PATH=<unset>`（`dmesg` 零子进程段错误）
- `gtk_modules_cleared` & `isolated_input_default_on`: `GTK_IM_MODULE=gtk-im-context-simple, GDK_CORE_DEVICE_EVENTS=1, XMODIFIERS=<unset>`
- `gio_module_dir_confined` & `webkit_zero_host_so`: `WebKitWebProcess` 映射宿主 `.so` 数量为 **0**，主进程无 `libgvfsdbus.so` 泄漏
- `startup_global_search_indexed`: 清空索引后首次启动扫描 `outcome=completed, reason=startup, indexed_item_count=175179, error=None`
- `renders_content`: `distinct_colors=45727, grey_stddev=23.5438`
- `x11_click_1_navigator` (`delta=0.0523`) & `x11_click_2_settings` (`delta=0.0215`): 真实 X11 点击切换「导航器」与「设置」页
- `quick_search_input_works`: 点击 Quick Search 并输入 `Desktop`（`{"open": true, "val": "Desktop", "activeTag": "INPUT"}`）
- `global_search_input_works`: 点击全局搜索并输入 `Desktop`（`{"disabled": false, "value": "Desktop", "driveGroups": 6, "headerText": "在 6 个驱动器上找到 239 项"}`）
- `language_combobox_opens`: 点击「设置 -> 常规 -> 语言」下拉框正常展开 17 种语言且聚焦输入框不卡死（`{"open": true, "optionCount": 17, "activeTag": "INPUT"}`）
- `theme_combobox_opens`: 点击「设置 -> 界面外观 -> 主题」下拉框正常展开（`{"open": true, "options": ["暗色", "亮色（开发中）", "系统"]}`）
- `no_gl_or_loader_errors_in_log`: `offending=[]`
