#!/usr/bin/env bash
# yt-tools plugin — SessionStart hook (POSIX / bash).
#
# Idempotent ensure-install: on every session start, verify that the
# yt-tools package is installed via pipx from this plugin's clone (the
# $CLAUDE_PLUGIN_ROOT directory) at the version declared in the clone's
# pyproject.toml, and that ffmpeg is on PATH. Print per-OS install hints
# when something is missing; never block session start (always exit 0,
# errors go to stderr).
#
# Windows users: if this hook fails to execute (e.g. no bash on PATH),
# run the PowerShell equivalent manually:
#   pwsh -File "$CLAUDE_PLUGIN_ROOT/scripts/ensure-install.ps1"

set -u

log() { printf '[yt-tools] %s\n' "$*" >&2; }

# 0. Resolve plugin root ───────────────────────────────────────────────────
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$PLUGIN_ROOT" ]; then
    log "CLAUDE_PLUGIN_ROOT not set — running outside plugin context; skipping."
    exit 0
fi
if [ ! -f "$PLUGIN_ROOT/pyproject.toml" ]; then
    log "Missing pyproject.toml at $PLUGIN_ROOT — plugin layout broken; skipping."
    exit 0
fi

# Parse declared version from the plugin's pyproject.toml (line: version = "X.Y.Z")
declared_version=$(awk -F'"' '/^version = / { print $2; exit }' "$PLUGIN_ROOT/pyproject.toml")
if [ -z "$declared_version" ]; then
    log "Could not parse version from $PLUGIN_ROOT/pyproject.toml; skipping."
    exit 0
fi

# 1. Resolve interpreter + pipx runner ─────────────────────────────────────
# probe_python is the candidate interpreter — used both for the pipx module
# fallback below and the health check / --python forwarding in step 2.
# YT_TOOLS_PYTHON overrides it.
probe_python="${YT_TOOLS_PYTHON:-}"
if [ -z "$probe_python" ]; then
    probe_python="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
fi

# Resolve how to invoke pipx. Prefer a pipx on PATH; otherwise fall back to
# `<python> -m pipx` for the common "pip install --user pipx done, but
# ensurepath not run / shell not restarted yet" case. Zero invasiveness — no
# PATH mutation, no auto-bootstrap of pipx itself.
if command -v pipx >/dev/null 2>&1; then
    pipx_run() { pipx "$@"; }
elif [ -n "$probe_python" ] && "$probe_python" -m pipx --version >/dev/null 2>&1; then
    log "pipx not on PATH; using '$probe_python -m pipx' (module fallback)."
    pipx_run() { "$probe_python" -m pipx "$@"; }
else
    log "pipx not found on PATH (and not importable as a module). Install it first:"
    log "  python -m pip install --user pipx"
    log "  python -m pipx ensurepath   # restart shell after"
    log "Then this hook will install yt-tools on the next session start."
    exit 0
fi

# 2. Probe yt-tools — install or update from plugin clone ──────────────────
installed_version=""
if pipx_run list --short 2>/dev/null | grep -q '^yt-tools-cli '; then
    installed_version=$(pipx_run list --short 2>/dev/null | awk '/^yt-tools-cli / {print $2}')
fi

needs_install=true
if [ -n "$installed_version" ] && [ "$installed_version" = "$declared_version" ]; then
    needs_install=false
    log "yt-tools $installed_version installed from plugin clone — ok"
fi

if [ "$needs_install" = "true" ]; then
    if [ -n "$installed_version" ]; then
        log "Installed $installed_version != plugin $declared_version; reinstalling from $PLUGIN_ROOT..."
    else
        log "yt-tools not installed; installing from $PLUGIN_ROOT via pipx..."
    fi

    # Python health probe — bail if the candidate interpreter's stdlib is
    # broken (e.g. uv-toolchain drift leaves SRE magic mismatch and re.compile
    # crashes). Without this we silently replace a working venv with a broken
    # one. probe_python was resolved in step 1 (YT_TOOLS_PYTHON overrides);
    # it is also forwarded to pipx via --python.
    if [ -n "$probe_python" ]; then
        if ! "$probe_python" -c "import re; re.compile('x')" >/dev/null 2>&1; then
            log "WARN: $probe_python failed health check (import re / re.compile crashed)."
            log "Refusing to install — would replace a working pipx venv with a broken interpreter."
            log "Fix: set YT_TOOLS_PYTHON=/path/to/known-good/python and start a new session,"
            log "or repair the system interpreter (uv toolchain refresh / pyenv rebuild)."
            exit 0
        fi
    fi

    # Uninstall first if a previous venv exists. pipx `install --force` with
    # the uv backend leaves a previous venv in place ("not created in this
    # session") and the reinstall silently no-ops, so we drop --force in
    # favour of explicit uninstall + clean install. Idempotent (silent if
    # nothing is installed).
    if [ -n "$installed_version" ]; then
        pipx_run uninstall yt-tools-cli >&2 2>/dev/null || \
            log "WARN: pipx uninstall yt-tools-cli failed; will attempt install anyway."
    fi

    # Install with [full] extras — core + [frames] + [audio] + bpm-detector
    # (chord progression + structure detection). [ocr] stays opt-in. If the
    # VCS dep fetch fails (corporate proxy blocking PEP 508 direct refs,
    # transient network), fall back to core + [frames,audio]: every flow
    # except [ocr] still works, Flow C runs the librosa-only path.
    pipx_args=(install)
    if [ -n "${YT_TOOLS_PYTHON:-}" ]; then
        pipx_args+=(--python "$YT_TOOLS_PYTHON")
    fi
    if ! pipx_run "${pipx_args[@]}" "${PLUGIN_ROOT}[full]" >&2; then
        log "WARN: install with [full] extras failed (likely bpm-detector VCS fetch blocked); falling back to [frames,audio] install."
        if ! pipx_run "${pipx_args[@]}" "${PLUGIN_ROOT}[frames,audio]" >&2; then
            log "WARN: [frames,audio] install also failed. Investigate pipx state."
        fi
    fi
fi

# 3. Probe ffmpeg ──────────────────────────────────────────────────────────
if ! command -v ffmpeg >/dev/null 2>&1; then
    case "$(uname -s 2>/dev/null || echo unknown)" in
        Darwin*)
            log "ffmpeg not on PATH. Install via Homebrew:  brew install ffmpeg"
            ;;
        Linux*)
            log "ffmpeg not on PATH. Install via your package manager:"
            log "  Debian/Ubuntu:  sudo apt install ffmpeg"
            log "  Fedora/RHEL:    sudo dnf install ffmpeg"
            log "  Arch:           sudo pacman -S ffmpeg"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            log "ffmpeg not on PATH (Windows). Install via winget:"
            log "  winget install Gyan.FFmpeg"
            log "Then restart the shell so the new PATH is picked up."
            ;;
        *)
            log "ffmpeg not on PATH. Install via your platform's package manager."
            ;;
    esac
fi

exit 0
