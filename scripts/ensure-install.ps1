# yt-tools plugin — SessionStart hook (PowerShell variant for Windows).
#
# Idempotent ensure-install: on every session start, verify that the
# yt-tools package is installed via pipx from this plugin's clone (the
# $env:CLAUDE_PLUGIN_ROOT directory) at the version declared in the clone's
# pyproject.toml, and that ffmpeg is on PATH. Print install hints when
# something is missing; never block session start (always exit 0, errors go
# to stderr).
#
# Invoked manually on Windows if hooks.json's POSIX command does not run:
#   pwsh -File "$env:CLAUDE_PLUGIN_ROOT/scripts/ensure-install.ps1"

$ErrorActionPreference = 'Continue'

function Write-PluginLog {
    param([string]$Message)
    [Console]::Error.WriteLine("[yt-tools] $Message")
}

# bpm-detector is enrichment for yt-listen (chord progression, structure,
# refined BPM/key) and is not on PyPI - so it cannot ride the published
# `[full]` extra: PyPI rejects PEP 508 direct references in Requires-Dist with
# "400 Can't have direct dependency" at upload time (release v0.23.0 died on
# exactly that). It is injected on top of the install instead, best-effort.
$BpmDetectorSpec = 'bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0'

# 0. Resolve plugin root ───────────────────────────────────────────────────
$PluginRoot = $env:CLAUDE_PLUGIN_ROOT
if (-not $PluginRoot) {
    Write-PluginLog 'CLAUDE_PLUGIN_ROOT not set - running outside plugin context; skipping.'
    exit 0
}
$pyprojectPath = Join-Path $PluginRoot 'pyproject.toml'
if (-not (Test-Path $pyprojectPath)) {
    Write-PluginLog "Missing pyproject.toml at $PluginRoot - plugin layout broken; skipping."
    exit 0
}

# Parse declared version (line:  version = "X.Y.Z")
$declaredVersion = $null
foreach ($line in Get-Content $pyprojectPath) {
    if ($line -match '^version\s*=\s*"([^"]+)"') {
        $declaredVersion = $Matches[1]
        break
    }
}
if (-not $declaredVersion) {
    Write-PluginLog "Could not parse version from $pyprojectPath; skipping."
    exit 0
}

# 1. Resolve interpreter + pipx runner ─────────────────────────────────────
# $probePython is the candidate interpreter — used for the pipx module fallback
# below and the health check / --python forwarding in step 2. YT_TOOLS_PYTHON
# overrides it.
$probePython = $env:YT_TOOLS_PYTHON
if (-not $probePython) {
    $probePython = (Get-Command python -ErrorAction SilentlyContinue).Source
}

# Resolve how to invoke pipx. Prefer a pipx on PATH; otherwise fall back to
# `<python> -m pipx` for the common "pip install --user pipx done, but
# ensurepath not run / shell not restarted yet" case. Zero invasiveness — no
# PATH mutation, no auto-bootstrap of pipx itself.
$script:PipxPython = $null
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    if ($probePython) {
        & $probePython -m pipx --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $script:PipxPython = $probePython
            Write-PluginLog "pipx not on PATH; using '$probePython -m pipx' (module fallback)."
        }
    }
    if (-not $script:PipxPython) {
        Write-PluginLog 'pipx not found on PATH (and not importable as a module). Install it first:'
        Write-PluginLog '  python -m pip install --user pipx'
        Write-PluginLog '  python -m pipx ensurepath   # restart shell after'
        Write-PluginLog 'Then this hook will install yt-tools on the next session start.'
        exit 0
    }
}

# Route every pipx call through this so the module fallback is transparent.
function Invoke-Pipx {
    if ($script:PipxPython) {
        & $script:PipxPython -m pipx @args
    } else {
        & pipx @args
    }
}

# 2. Probe yt-tools — install or update from plugin clone ──────────────────
$installedVersion = $null
try {
    $pipxOut = Invoke-Pipx list --short 2>$null
    if ($pipxOut) {
        $line = $pipxOut | Where-Object { $_ -match '^yt-tools-cli\s+' } | Select-Object -First 1
        if ($line) {
            $installedVersion = ($line -split '\s+')[1]
        }
    }
} catch {
    # pipx list error — assume yt-tools missing
}

$needsInstall = $true
if ($installedVersion -and $installedVersion -eq $declaredVersion) {
    $needsInstall = $false
    Write-PluginLog "yt-tools $installedVersion installed from plugin clone - ok"
}

if ($needsInstall) {
    if ($installedVersion) {
        Write-PluginLog "Installed $installedVersion != plugin $declaredVersion; reinstalling from $PluginRoot..."
    } else {
        Write-PluginLog "yt-tools not installed; installing from $PluginRoot via pipx..."
    }

    # Python health probe — bail if the candidate interpreter's stdlib is
    # broken (e.g. uv-toolchain drift leaves SRE magic mismatch and re.compile
    # crashes). Without this we silently replace a working venv with a broken
    # one. $probePython was resolved in step 1 (YT_TOOLS_PYTHON overrides);
    # it is also forwarded to pipx via --python.
    if ($probePython) {
        & $probePython -c "import re; re.compile('x')" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-PluginLog "WARN: $probePython failed health check (import re / re.compile crashed)."
            Write-PluginLog 'Refusing to install - would replace a working pipx venv with a broken interpreter.'
            Write-PluginLog 'Fix: set $env:YT_TOOLS_PYTHON to a known-good python.exe path and restart the session,'
            Write-PluginLog 'or repair the system interpreter (uv toolchain refresh).'
            exit 0
        }
    }

    # Uninstall first if a previous venv exists. pipx `install --force` with
    # the uv backend leaves a previous venv in place ("not created in this
    # session") and the reinstall silently no-ops, so we drop --force in
    # favour of explicit uninstall + clean install. Idempotent (silent if
    # nothing is installed).
    if ($installedVersion) {
        Invoke-Pipx uninstall yt-tools-cli 2>&1 | ForEach-Object { Write-PluginLog $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-PluginLog 'WARN: pipx uninstall yt-tools-cli failed; will attempt install anyway.'
        }
    }

    # Install with [full] extras - core + [frames] + [audio]. [ocr] stays
    # opt-in, and bpm-detector is injected separately ($BpmDetectorSpec at the
    # top): it cannot live in the metadata.
    $pipxArgs = @('install')
    if ($env:YT_TOOLS_PYTHON) {
        $pipxArgs += @('--python', $env:YT_TOOLS_PYTHON)
    }
    $installOk = $true
    $fullTarget = "$PluginRoot[full]"
    Invoke-Pipx @pipxArgs $fullTarget 2>&1 | ForEach-Object { Write-PluginLog $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-PluginLog 'WARN: install with [full] extras failed; falling back to [frames,audio] install.'
        $installOk = $false
        $fallbackTarget = "$PluginRoot[frames,audio]"
        Invoke-Pipx @pipxArgs $fallbackTarget 2>&1 | ForEach-Object { Write-PluginLog $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-PluginLog 'WARN: [frames,audio] install also failed. Investigate pipx state.'
        } else {
            $installOk = $true
        }
    }

    # bpm-detector on top of [full] - best-effort. A blocked VCS fetch
    # (corporate proxy, transient network) must leave a working install: the
    # enrichment is chord/structure/BPM refinement, not a flow of its own, and
    # yt-listen already renders the librosa-only path with explicit n/a markers.
    if ($installOk) {
        Invoke-Pipx inject yt-tools-cli $BpmDetectorSpec 2>&1 | ForEach-Object { Write-PluginLog $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-PluginLog 'WARN: bpm-detector inject failed (VCS fetch blocked?); yt-listen runs the librosa-only path.'
        }
    }
}

# 3. Probe ffmpeg ──────────────────────────────────────────────────────────
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-PluginLog 'ffmpeg not on PATH. Install via winget:'
    Write-PluginLog '  winget install Gyan.FFmpeg'
    Write-PluginLog 'Then restart the shell so the new PATH is picked up.'
}

exit 0
