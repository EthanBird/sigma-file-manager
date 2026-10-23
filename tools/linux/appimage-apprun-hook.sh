# zz-sfm-compat.sh
#
# AppImage launcher hardening, derived from a UOS Desktop 20 packaging
# post-mortem for a sibling Tauri application.  Only the parts that are
# meaningful for a self-contained AppImage are implemented here; the private
# loader / fixed-install-root part of that post-mortem cannot be expressed in
# an AppImage at all (the mount point is random, and WebKit helper processes
# are exec'd by the system loader), so it is deliberately absent.
#
# Sourced by AppRun AFTER apprun-hooks/linuxdeploy-plugin-gtk.sh.
#
# Overrides (all optional):
#   SFM_GL=auto|hardware|software   GL/EGL stack selection        (default auto)
#   SFM_ISOLATED_INPUT=1            full DDE-style input isolation (default 0)
#   SFM_KEEP_GTK_MODULES=1          keep session GTK_MODULES     (default 0)
#   SFM_KEEP_AT_BRIDGE=1            keep the AT-SPI2 bridge      (default 0)
#   SFM_DEBUG=1                     explain the GL decision on stderr

sfm_debug() {
    [ "${SFM_DEBUG:-0}" = "1" ] && printf 'sfm: %s\n' "$*" >&2
    return 0
}

# ---------------------------------------------------------------------------
# 1. GTK module isolation
#
# The AppImage carries its own GTK 3 / GLib.  A desktop session may inject
# GTK_MODULES=gail:atk-bridge, GTK3_MODULES=atk-bridge, its own GTK_PATH and an
# AT-SPI2 bridge.  Loading host modules into the bundled GTK mixes two GTK/GLib
# generations in one process; the failure mode observed downstream is that the
# window keeps responding to the window manager while every widget, input field
# and IPC entry point inside the page stops working.
# ---------------------------------------------------------------------------
export GTK_PATH="$APPDIR/usr/lib/gtk-3.0"
export GTK_EXE_PREFIX="$APPDIR/usr"

if [ "${SFM_KEEP_GTK_MODULES:-0}" = "1" ]; then
    sfm_debug "keeping session GTK_MODULES='${GTK_MODULES:-}' GTK3_MODULES='${GTK3_MODULES:-}'"
else
    # shellcheck disable=SC2153
    sfm_debug "clearing session GTK_MODULES='${GTK_MODULES:-}' GTK3_MODULES='${GTK3_MODULES:-}'"
    unset GTK_MODULES GTK3_MODULES
fi

if [ "${SFM_KEEP_AT_BRIDGE:-0}" = "1" ]; then
    sfm_debug "keeping AT-SPI2 bridge"
else
    export NO_AT_BRIDGE=1
fi

# Full DDE-style isolation: the bundled GTK uses its built-in input context and
# GDK consumes only X11 core events.  Enabled automatically on Deepin/UOS DDE
# or fcitx sessions where focusing an <input> (Quick Search, BaseCombobox in
# Settings, Global Search) otherwise deadlocks the WebKitGTK input loop; pass
# SFM_ISOLATED_INPUT=0 to opt out, or SFM_ISOLATED_INPUT=1 to force it on.
sfm_needs_isolated_input() {
    case "${SFM_ISOLATED_INPUT:-auto}" in
        1|true|yes) return 0 ;;
        0|false|no) return 1 ;;
    esac
    case "${XDG_CURRENT_DESKTOP:-}:${DESKTOP_SESSION:-}:${GTK_IM_MODULE:-}:${XMODIFIERS:-}" in
        *[Dd]eepin*|*[Uu][Oo][Ss]*|*fcitx*) return 0 ;;
    esac
    return 1
}

if sfm_needs_isolated_input; then
    unset XMODIFIERS QT_IM_MODULE SDL_IM_MODULE CLUTTER_IM_MODULE
    export GTK_IM_MODULE=gtk-im-context-simple
    export GDK_CORE_DEVICE_EVENTS=1
    sfm_debug "isolated input: GTK_IM_MODULE=gtk-im-context-simple GDK_CORE_DEVICE_EVENTS=1"
fi

# ---------------------------------------------------------------------------
# 2. GL / EGL stack selection
#
# The AppImage intentionally does not bundle libGL/libEGL: on a normal desktop
# the host stack gives hardware acceleration.  On a host without any usable GL
# driver (no DRI node and no Mesa software rasteriser, e.g. a minimal container
# or a locked-down enterprise image) WebKit aborts with
#   "Could not create default EGL display: EGL_BAD_PARAMETER"
# and the window stays blank forever.  For that case the AppImage ships an
# isolated, fixed-version Mesa software-rendering closure in
# usr/lib/sfm-softgl and switches to it.
# ---------------------------------------------------------------------------
sfm_egl_vendor_manifest() {
    for d in /usr/share/glvnd/egl_vendor.d \
             /usr/local/share/glvnd/egl_vendor.d \
             /etc/glvnd/egl_vendor.d; do
        if [ -d "$d" ] && ls "$d"/*.json >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

sfm_host_gl_usable() {
    # A DRM node means the kernel exposed a GPU and a driver claimed it.
    if [ -d /dev/dri ]; then
        if sfm_egl_vendor_manifest; then
            sfm_debug "host GL: /dev/dri + EGL vendor manifest -> hardware"
        else
            # GPU node but nothing to drive EGL through: this is the
            # "unsupported / proprietary driver" shape.  Stay on hardware but
            # apply upstream's documented mitigation, and leave SFM_GL=software
            # as the escape hatch when even that is not enough.
            export WEBKIT_DISABLE_COMPOSITING_MODE=1
            sfm_debug "host GL: /dev/dri without an EGL vendor manifest -> hardware, WebKit compositing disabled"
        fi
        return 0
    fi

    # No GPU node.  What matters for EGL is a GLVND vendor manifest: the
    # dispatcher is useless without one.  A DRI driver on its own is NOT
    # enough — a host with swrast_dri.so but no vendor manifest still dies
    # with "Could not create default EGL display: EGL_BAD_PARAMETER".
    if sfm_egl_vendor_manifest; then
        sfm_debug "host GL: EGL vendor manifest present -> hardware"
        return 0
    fi

    sfm_debug "host GL: no DRI node and no EGL vendor manifest -> software fallback"
    return 1
}

sfm_enable_software_gl() {
    local gl="$APPDIR/usr/lib/sfm-softgl"

    if [ ! -d "$gl" ]; then
        sfm_debug "software renderer requested but $gl is missing"
        return 0
    fi

    export LIBGL_ALWAYS_SOFTWARE=1
    export GALLIUM_DRIVER=llvmpipe
    export LIBGL_DRIVERS_PATH="$gl/dri"
    export __EGL_VENDOR_LIBRARY_FILENAMES="$gl/50_mesa.json"

    # LD_LIBRARY_PATH is used only on this fallback path so that the host GL
    # stack keeps winning by default.  The closure contains no libc/libstdc++,
    # so a system child process that inherits it cannot end up on a foreign
    # C library.
    export LD_LIBRARY_PATH="$gl:$APPDIR/usr/lib:${LD_LIBRARY_PATH:-}"

    # llvmpipe composite paths are expensive; skip WebKit's GL compositing.
    export WEBKIT_DISABLE_COMPOSITING_MODE=1

    sfm_debug "software GL enabled from $gl"
}

case "${SFM_GL:-auto}" in
    software)
        sfm_enable_software_gl
        export SFM_GL_ACTIVE=software
        ;;
    hardware)
        sfm_debug "SFM_GL=hardware: leaving the host GL stack alone"
        export SFM_GL_ACTIVE=hardware
        ;;
    *)
        if sfm_host_gl_usable; then
            export SFM_GL_ACTIVE=hardware
        else
            sfm_enable_software_gl
            export SFM_GL_ACTIVE=software
        fi
        ;;
esac
