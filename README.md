# yt-tools

CLI suite for **iterative agent-driven YouTube watching**. The agent reads the
transcript, decides which moments matter, then pulls only those frames or
audio FFT slices — no bulk download, no JSON soup, no MCP scaffolding.

Eight CLIs, one cache, stdout-friendly absolute paths so the caller never
has to guess where the artefact landed:

- `yt-search` — query → markdown list of candidate videos via yt-dlp's
  native `ytsearch` extractor. The entry-point when the user hasn't named
  a URL yet; pick one and continue into the other CLIs. Zero config, no
  API key.
- `yt-transcript` — clean markdown transcript with metadata header and
  `[mm:ss]` paragraph anchors copy-paste-friendly for `--timestamps`.
- `yt-meta` — full metadata as markdown: description, chapters (`[mm:ss]`
  anchors), most-replayed heatmap, view/like/comment counts, tags, subtitle
  languages. Zero added cost — reuses the same `yt-dlp --dump-json` call.
- `yt-comments` — comments as markdown (top-level + nested replies). A
  **separate paginated scrape** (can take minutes on viral videos), so it is
  capped at the top 50 by default; raise with `--max`. Call it deliberately.
- `yt-frames` — targeted frame extraction by timestamp, scene-detect, or
  fixed interval.
- `yt-ocr` — OCR over cached frames (RapidOCR PP-OCRv5 via onnxruntime) →
  `[mm:ss]` markdown blocks. The fallback for silent-with-text videos
  where the transcript is empty and content lives in burned-in overlay
  text (schematic labels, chord matrices, parameter walkthroughs). Needs
  the `[ocr]` extra.
- `yt-listen` — FFT audio analysis: per-timestamp clip + mel-spectrogram PNG +
  features `.md` with BPM, key, chord progression, and spectral statistics.
- `yt-watch` — combined transcript + scene-frames in one `.md` with
  `![](frames/...)` sidecar embeds.
- `yt-tools cache list | prune` — manage the source-mp4 cache.

## Installation

`yt-tools` v1 ships **via a Claude Code plugin** (recommended for agent
workflows) or **directly from this Git repository** (for standalone CLI
use in any environment). PyPI distribution is deferred to a future release.

### As a Claude Code plugin (recommended for agent workflows)

```text
/plugin marketplace add kzntsv-dev/claude-plugins
/plugin install yt-tools@opeitcloc03-claude-plugins
```

The plugin's `SessionStart` hook installs the package from its own clone —
`pipx install "$CLAUDE_PLUGIN_ROOT[full]"`, with an explicit reinstall
whenever the version in the clone's `pyproject.toml` changes — exposing
`yt-search`, `yt-transcript`, `yt-meta`, `yt-comments`,
`yt-frames`, `yt-ocr`, `yt-listen`, `yt-watch`, `yt-tools` (and a shimmed
`yt-dlp`) in `~/.local/bin/`. The bundled `using-yt-tools` skill
orchestrates the flows (discovery search / iterative watch / targeted
frames / audio analysis / metadata / comments / OCR) for the agent.

The hook installs the `[full]` extra by default: core + `[frames]` +
`[audio]` + `bpm-detector`, so every flow except OCR works on a fresh
install. The `[ocr]` extra (RapidOCR + onnxruntime, plus a ~10 MB model
downloaded lazily on first run) is always opt-in — see the OCR section
below. If the `bpm-detector` VCS fetch is blocked (proxy), the hook falls
back to `[frames,audio]`: everything still works, `yt-listen` just uses its
librosa-only path.

The plugin marketplace catalog lives at
[`kzntsv-dev/claude-plugins`](https://github.com/kzntsv-dev/claude-plugins);
this repository at [`kzntsv-dev/yt-tools`](https://github.com/kzntsv-dev/yt-tools).

#### Using the skill from another harness

The bundled `using-yt-tools` skill is a **portable pointer**: it describes the
CLI flows (resolve binaries → run a flow → read the artefact) and needs no
harness-specific tooling. Any host that loads an Agent Skills `SKILL.md` can use
it. This repo ships one such wiring, for `pi`:

```json
// .pi/settings.json — paths resolve relative to .pi/
{ "skills": ["../skills"] }
```

Project resources need trust: run non-interactively with `pi -a -p "…"`, or
`/trust` once in an interactive session. For other hosts, point their skill
directory at this repo's `skills/` (or symlink `skills/using-yt-tools` into it).

### Standalone CLI (any environment)

Install directly from this Git repo via [`pipx`](https://pipx.pypa.io/):

```bash
# 1. bootstrap pipx (one-time per user)
python -m pip install --user pipx
python -m pipx ensurepath          # adds ~/.local/bin to PATH; restart shell after

# 2. install yt-tools (core) from this repo
pipx install git+https://github.com/kzntsv-dev/yt-tools.git

# 3. add the extras you need — one combined install re-creates the same venv
pipx install --force "git+https://github.com/kzntsv-dev/yt-tools.git#egg=yt-tools[full]"
```

#### What each extra buys you

A plain install is deliberately light: it never pulls OpenCV, librosa or
matplotlib. Heavier stacks are opt-in, per flow.

| Install | Pulls in | Unlocks |
|---|---|---|
| core (no extras) | `youtube-transcript-api`, `yt-dlp` | `yt-transcript`, `yt-meta`, `yt-comments`, `yt-search`, `yt-tools cache`; `yt-frames --timestamps` / `--mode interval` (without near-duplicate dedup) |
| `[frames]` | `scenedetect[opencv]` (OpenCV) | `yt-frames --mode scene`, `yt-watch`, and dedup in the other frame modes |
| `[audio]` | `librosa`, `matplotlib` | `yt-listen` (spectral features, spectrogram, BPM/key) |
| `[ocr]` | `rapidocr`, `onnxruntime` | `yt-ocr` (PP-OCRv5; ONNX model downloaded on first run) |
| `[full]` | core + `[frames]` + `[audio]` + `bpm-detector` | everything above except `yt-ocr` — this is what the plugin hook installs |

`[full]` adds
[`bpm-detector`](https://github.com/libraz/bpm-detector) (VCS dep, not yet
on PyPI) on top of `[audio]`: chord progression, structural segments,
refined BPM and a confidence-scored key per timestamp. Without it,
`yt-listen` gracefully falls back to librosa-only basics.

> **Adding an extra to an existing pipx venv.** Re-run `pipx install --force`
> with the full extras list (as in step 3) rather than installing a second
> time — a plain reinstall of a different extras set replaces the venv. Use
> `pipx inject yt-tools scenedetect opencv-python` only when you cannot
> re-run the installer.

#### What happens when an extra is missing

A heavy stack is only ever needed by the flow that uses it, so a missing extra
is answered by that flow — never by an `ImportError` traceback, and never by a
silent quality drop:

| Missing | Flow | Behaviour |
|---|---|---|
| `[frames]` | `yt-frames --mode scene`, `yt-watch` | **Refused**, exit code 1, before anything is downloaded: `yt-frames --mode scene requires the [frames] extra:` + both install commands. |
| `[frames]` | `yt-frames --timestamps` | **Works.** An explicit timestamp list is never deduplicated or capped, so it needs no heavy stack at all. |
| `[frames]` | `yt-frames --mode interval` | **Works**, but the skipped near-duplicate dedup is announced on stderr (`note: near-duplicate dedup requires the [frames] extra: …`) — the caller must be able to tell de-duplicated frames from raw ones. |
| `[audio]` | `yt-listen` | **Refused**, exit code 1, with the `[audio]` install commands. |
| `[ocr]` | `yt-ocr` | **Refused**, exit code 1, with the `[ocr]` install commands. |

The same wording is produced by one formatter for every CLI, so the message a
human sees and the message an agent parses are always the same shape: reason,
`pipx inject` line, `pip install` line.

### Check your environment first: `yt-tools doctor`

On a fresh machine (or when a flow failed for an unclear reason), `yt-tools doctor`
is a **read-only** preflight: what is present, what is missing, and the one exact
command worth running next. It installs nothing — it never even creates the cache
directory it inspects.

```console
$ yt-tools doctor
yt-tools doctor - environment preflight (yt-tools 0.17.0)

  warn     python        3.13.1 is above the supported range >=3.10,<3.13 (untested; ...)
  ok       yt-dlp        /usr/local/bin/yt-dlp
  missing  ffmpeg        not found on PATH (looked for 'ffmpeg')
                         -> sudo apt install ffmpeg
  ok       extra:frames  installed - yt-frames --mode scene and yt-watch
  missing  extra:audio   not installed - yt-listen is unavailable
                         -> pipx inject yt-tools librosa matplotlib
  ok       cache         ./yt-cache - 5 videos, 75.6 MB
  ok       version       0.17.0 - yt_tools.__version__, pyproject.toml, plugin.json, SKILL.md agree

can_proceed: no
next_step: sudo apt install ffmpeg
```

Every check carries a `status` (`ok` | `warn` | `missing`); `warn` is a
recommendation, never a blocker — an unsupported interpreter or an absent
`[audio]` still leaves the light flows alive. A **missing external binary**
(`yt-dlp`, `ffmpeg`) is a blocker. The exit code is `0` when `can_proceed` is
true and `1` when a blocker stands.

The cache check asks "could a cached flow write here?" by *trying*: it creates a
throwaway file inside the directory and removes it again, because on Windows
`os.access` does not report directory permissions at all. A cache that is
unreadable, unwritable, or blocked by a file in its place is a `warn` carrying
the reason — a preflight that exists to explain a broken environment must not
die inside one.

`--json` prints the same report for machines — same `status` values, same
`can_proceed` / `next_step` fields, same exit code — so an agent can branch on it
without parsing prose:

```bash
yt-tools doctor --json | jq '{can_proceed, next_step}'
```

### ffmpeg (external binary, all install paths)

`yt-tools` shells out to `ffmpeg` for source-video caching and per-clip
extraction. Install via your OS package manager:

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux (Debian/Ubuntu) | `sudo apt install ffmpeg` |
| Linux (Fedora/RHEL) | `sudo dnf install ffmpeg` |

> **Windows-gotcha.** `winget install Gyan.FFmpeg` writes `ffmpeg.exe` into
> the per-user PATH, which the *current* shell session does not re-read.
> Either restart the terminal, or prepend the install directory to `$env:PATH`
> for the current session.

### Plugin hook environment variables

The bundled `SessionStart` hook honours two environment variables, both
optional:

| Var | Effect |
|---|---|
| `YT_TOOLS_PYTHON` | Full path to a Python interpreter. Used both for the pre-install health probe and forwarded to `pipx install --python` so the venv is built with this exact interpreter. Set this when your default Python is broken (e.g. `uv` toolchain drift surfacing `SRE module mismatch` from `re.compile`). |
| `CLAUDE_PLUGIN_ROOT` | Set automatically by Claude Code to the plugin's local clone; the hook uses it as the install source. Not for manual override. |

Before each install, the hook probes the candidate interpreter with
`python -c "import re; re.compile('x')"`. If the probe crashes (broken
stdlib), the hook refuses to install and preserves any existing pipx-venv
rather than replacing it with a broken one.

## Quick start

First run on a new machine: `yt-tools doctor` — it names what is missing (and the
command that fixes it) before a flow fails halfway through.

The CLIs are designed for an **iterative** loop: cheap transcript first,
then targeted heavy fetches only at the timestamps that mattered.

### Flow 0 — discovery

```bash
# Find candidate videos when you don't have a URL yet.
yt-search "MakeNoise Maths tutorial"
# → ./yt-cache/_search/makenoise-maths-tutorial-1748443391123456789.md

yt-search "drum tutorial" --max 5                    # cap result count
yt-search "long-form review" --min-duration 20:00    # skip shorts
yt-search "explainer" --max-duration 10:00           # skip long-form
```

The result file is per-result blocks — title, channel, duration, views,
URL (last field per block, so `grep -oP 'https://[^\s]+'` peels the raw
URL list). Pick one and continue into Flow 1 (`yt-transcript`), Flow 2
(`yt-meta`), or any of the others. Absolute upload date is **not** in
the result file (YouTube only exposes relative dates — "2 weeks ago" —
on the search results page, and `--flat-playlist` skips the per-video
round-trip); fetch `yt-meta` on the chosen URL if you need it.

> Files land in `yt-cache/_search/<slug>-<unix-ns>.md` (outside any
> `<video-id>/`) — the nanosecond timestamp guarantees re-running the
> same query never overwrites the previous run, even in the same second.
> YouTube's search ranking is not stable between calls; treat the file
> as a snapshot, not a cache.

### Flow 1 — transcript-driven frames

```bash
# 1) get the transcript; agent reads markdown and notes timestamps
yt-transcript https://www.youtube.com/watch?v=dQw4w9WgXcQ
# → ./yt-cache/dQw4w9WgXcQ/transcript.md

# 2) pull only the frames you actually need
yt-frames https://www.youtube.com/watch?v=dQw4w9WgXcQ --timestamps 0:43,1:23,2:30
# Wrote: ./yt-cache/dQw4w9WgXcQ/frames/frame_0043.jpg
# Wrote: ./yt-cache/dQw4w9WgXcQ/frames/frame_0123.jpg
# Wrote: ./yt-cache/dQw4w9WgXcQ/frames/frame_0230.jpg
```

When a video has no usable captions, `yt-transcript` says so instead of leaking a
library traceback: it exits non-zero with a named reason — `captions are disabled
for this video`, `no captions in the requested language(s): ru, en`, `the
transcript API returned nothing usable (ParseError: …)` — and points at the two
fallbacks, `yt-listen` (audio) and `yt-ocr` (text burned into the frames). `yt-watch`
does not fail on the same video: it writes a **frames-only** `watch.md`, says why in
the header, and warns on stderr.

### Flow 2 — video metadata

```bash
# Description, chapters, most-replayed, counts, tags — one markdown file.
# Free: same yt-dlp --dump-json the transcript path already runs.
yt-meta https://www.youtube.com/watch?v=dQw4w9WgXcQ
# → ./yt-cache/dQw4w9WgXcQ/meta.md
```

The chapter and most-replayed anchors are `[mm:ss]`, so they paste straight
into `yt-frames --timestamps` / `yt-listen --timestamps`.

### Flow 3 — comments

```bash
# Top 50 comments (with replies) as markdown. Separate paginated scrape —
# slower than the others; bump the cap only when you need it.
yt-comments https://www.youtube.com/watch?v=dQw4w9WgXcQ
# → ./yt-cache/dQw4w9WgXcQ/comments.md

yt-comments URL --max 200          # pull more (slower)
yt-comments URL --sort new         # newest-first instead of top
```

> **Cost note:** comments are *not* part of `yt-meta` and are *not* free —
> each run paginates YouTube's comment feed and can take minutes on viral
> videos. Invoke it only when comments are actually what you need.

### Flow 4 — audio FFT analysis at specific moments

```bash
# Per timestamp: clip.wav + spectrum.png + features.md (BPM, key, chord, MFCC, etc.)
yt-listen https://www.youtube.com/watch?v=dQw4w9WgXcQ --timestamps 0:30 --duration 8s
# Wrote: ./yt-cache/dQw4w9WgXcQ/audio/clip_0030.wav
# Wrote: ./yt-cache/dQw4w9WgXcQ/audio/spectrum_0030.png
# Wrote: ./yt-cache/dQw4w9WgXcQ/audio/features_0030.md
```

### Flow 5 — scene-driven bulk frames

```bash
# Or combined transcript + scene-frames in one self-contained .md:
yt-watch URL
```

Auto-selected frames (both `--mode scene` and `--mode interval`) are capped at
**100 per call** by default and evenly thinned, always keeping the first and the
last frame — a cut-heavy clip cannot hand the agent hundreds of frames, and a
long clip keeps its tail. `--max-frames N` changes the budget; `--max-frames 0`
disables the cap. An explicit `--timestamps` list is never capped or thinned.
When thinning happens it is reported on stderr.

`yt-watch` embeds auto-selected frames, so it spends the same budget: the same
default of 100, the same even thinning, and the same `--max-frames` /
`--no-dedup` flags. `watch.md` links only the frames that survived — the ones a
budget dropped are removed from disk, so every `![](frames/…)` link is a file the
agent can read, and a cut-heavy clip can no longer turn into a 300-frame document.
Its stdout stays one line (the path of `watch.md`); counts and the fallback
warning go to stderr.

A static clip — talking head, screencast, one long take — has almost no cuts, so
scene detection would hand the agent a single frame for the whole video. When
fewer than 8 scenes are detected, the same budget is spent on an even scan of the
timeline instead, and the fallback is announced on stderr: the agent must know it
is looking at a uniform scan rather than at scenes.

Near-duplicate candidates — the same slide held on screen, a terminal that only
scrolls — are dropped **before** the cap is applied, so the budget goes to frames
that actually differ. The metric is a 16×16 grayscale thumbnail compared by
mean-abs-diff against the last *kept* frame, threshold 2.0/255; it is fail-open
(a frame that cannot be read is kept, never lost). `--no-dedup` turns the pass
off. Dropped copies and thinned frames are both reported on stderr, and frames the
agent never gets are removed from disk rather than announced as `Wrote:`.

### Flow F — OCR (silent-with-text videos)

When `yt-transcript` returns 0 bytes (or near-nothing) and the content
lives entirely in burned-in overlay text — tutorial channels with
schematic labels, chord matrices over dimmed B-roll, parameter
walkthroughs without voice-over — fall back to OCR over cached frames.
Engine is [RapidOCR](https://github.com/RapidAI/RapidOCR) (PP-OCRv5
models via `onnxruntime`).

```bash
# 1) extract frames first (lower scene threshold catches overlay fades
#    within the same shot; or use --mode interval for a denser sample)
yt-frames URL --mode scene --scene-threshold 12
# or:
yt-frames URL --mode interval --interval 15s

# 2) OCR every cached frame → markdown with [mm:ss] anchors
yt-ocr URL
# → ./yt-cache/<vid>/ocr.md

# Or skip step 1 — let yt-ocr extract on its own:
yt-ocr URL --timestamps 1:30,2:45,5:10

# Non-English overlays:
yt-ocr URL --language ru        # Cyrillic
yt-ocr URL --language ja        # Japanese
yt-ocr URL --language zh        # Chinese (simplified)
yt-ocr URL --language multi     # PP-OCR multilingual (Chinese+English)
```

The result file mirrors `transcript.md`: per-`[mm:ss]` block, one line
per detected text region, an explicit `_(no text detected)_` marker for
frames where the engine returned nothing (so an agent can tell the frame
was checked vs. silently omitted).

> **Install the `[ocr]` extra.** RapidOCR and `onnxruntime` are **not**
> in the core install. If `yt-ocr` exits with the missing-extra hint,
> run:
> ```bash
> pipx inject yt-tools rapidocr onnxruntime
> # or for non-pipx setups:
> pip install 'yt-tools[ocr]'
> ```
> First run with a given `--language` lazy-downloads a ~10 MB PP-OCRv5
> ONNX model into `~/.cache/rapidocr/`.

### Cache hygiene

```bash
yt-tools cache list                      # show cached source-mp4 sizes
yt-tools cache prune --older-than 7d     # drop sources older than a week
```

`cache list` is honest about what it could not read: an entry — and the total —
that includes an unreadable subtree is marked `(lower bound)`, because the real
size is larger. An unreadable `yt-cache` itself is not a traceback: both
subcommands name the cause (`cannot be read (Permission denied)`) and exit
non-zero, the same way `yt-tools doctor` explains it.

## Output layout

Per video, all artefacts land under `./yt-cache/<video-id>/` in the current
working directory; the one exception is `yt-search`, which writes to
`./yt-cache/_search/` because its output isn't tied to a single video:

```
./yt-cache/
  _search/
    <slug>-<unix>.md       # yt-search output (one per query invocation)
  <video-id>/
    source.mp4             # cached source (≤720p, reused by yt-frames / yt-listen / yt-watch)
    transcript.md          # yt-transcript output
    meta.md                # yt-meta output
    comments.md            # yt-comments output
    ocr.md                 # yt-ocr output (re-reads frames/ — same dir as yt-frames)
    watch.md               # yt-watch output
    frames/
      frame_<mmss>.jpg     # yt-frames / yt-watch (zero-padded mmss)
      frame_<mmss>_<mmm>.jpg  # sub-second moment: milliseconds in the name (issue:74)
    audio/
      clip_<mmss>.wav      # yt-listen per-timestamp clip
      spectrum_<mmss>.png  # yt-listen mel-spectrogram
      features_<mmss>.md   # yt-listen feature report (BPM / key / chord / MFCC / spectral stats)
```

The last line of stdout (or one line per artefact for multi-output commands
like `yt-frames` and `yt-listen`) is the absolute path of the produced file,
prefixed with `Wrote: `. This makes piping into agent tooling or shell
scripts trivial.

## Defaults

- **Cache directory:** `./yt-cache/<video-id>/` in cwd (`.gitignore`-friendly).
- **Source caching:** on by default (`--no-cache-source` to stream via
  `yt-dlp -g | ffmpeg`).
- **Frame cache key:** a frame file is named after its moment, so `frames/` is a
  cache keyed by timestamp: `yt-watch` re-uses a file that is already there. Names
  written before 0.11.0 were per *second* (a later frame of that second could have
  overwritten an earlier one) — delete `yt-cache/<id>/frames/` once after upgrading
  if you want those seconds re-extracted from scratch.
- **Combined embed (`yt-watch`):** sidecar `![](frames/frame_<mmss>.jpg)`,
  never base64 (avoids ~33 % token bloat). A frame name is a pure function of its
  timestamp — `frame_0130.jpg` for 90.0 s, `frame_0130_250.jpg` for 90.25 s — so two
  timestamps in one second can never overwrite each other, and the number of
  `Wrote:` lines equals the number of files on disk. Embedded frames obey the
  `--max-frames` / `--no-dedup` budget (default 100, dedup on), and only the
  survivors are linked — dropped candidates are deleted, so a `watch.md` from an
  earlier run may reference a frame this run removed; re-run `yt-watch` to
  regenerate it.
- **No captions (`yt-transcript`):** a named reason + both fallbacks (`yt-listen`,
  `yt-ocr`), exit code 1, no artefact written. `yt-watch` degrades instead —
  frames-only `watch.md` with the reason in its header (exit code 0), because the
  frames are already extracted and are the bulk of the artefact. An unexpected bug
  in the transcript path is still fatal.
- **Distill (`yt-transcript --distill`):** prints a hint to invoke an external
  distillation step (`mcp__interns__transcript_distill` in Claude Code); the
  CLI itself never calls an LLM.
- **Scene threshold:** 27 (PySceneDetect `ContentDetector` default; lower =
  more sensitive).
- **`yt-listen` defaults:** 8-second clip per timestamp, mel-spectrogram at
  default `librosa` settings, full feature set when `[full]` extra is
  installed.

## Requirements

- Python ≥ 3.10, < 3.13 (`librosa` Py 3.13 friction holds the ceiling)
- `ffmpeg` on PATH (external binary, see Installation)
- `pipx` recommended for install (any pip-compatible installer works)

## Design choices

**No MCP wrapper.** Each CLI is a stateless one-shot transform: URL → artefact.
There is no typed schema discovery, no shared cache across sessions, no
persistent connection an MCP server would benefit from. `Bash` is the right
caller.

**No Whisper / Gemini for transcription.** `yt-transcript` uses YouTube's
`auto-sub` via `youtube-transcript-api`. Whisper would add a massive runtime
dep for a marginally cleaner transcript; the iterative flow tolerates auto-sub
quality and benefits more from `--distill` on the markdown than from a heavier
STT step.

**Iterative > bulk.** The primary flow is "transcript first, then targeted
heavy fetches". Scene-mode and interval-mode are secondary, opt-in via flags.

## Development

```bash
git clone https://github.com/kzntsv-dev/yt-tools.git
cd yt-tools
pip install -e ".[full,test]"
pytest tests/
```

### Releasing a version

The version lives in exactly one place — `pyproject.toml`. `yt_tools.__version__`
reads the installed distribution metadata (falling back to `pyproject.toml` when
run straight from a checkout), and the two derived manifests are written by a
script, never by hand:

```bash
# 1. bump `version` in pyproject.toml, then:
python scripts/sync-version.py          # .claude-plugin/plugin.json + SKILL.md frontmatter
python scripts/sync-version.py --check  # verify only (this is what the guard test runs)
git commit -m "feat(...): ... (vX.Y.Z)"
```

`tests/test_version.py` fails when the four disagree, so a half-finished bump
cannot ship: the plugin marketplace cannot advertise one version while pipx
installs another.

Test layer covers pure logic (video-id extraction, mm:ss conversions,
snippets → markdown rendering, cache list/prune, interleaved rendering,
subprocess failure formatting) and CLI smoke (transcript / frames / listen /
watch end-to-end with mocked subprocess calls). YouTube + `yt-dlp` + `ffmpeg`
are mocked at the smoke layer — no network is touched.

### The core-install gate (needs docker)

```bash
bash scripts/check-core-install.sh          # YT_SRC=<other checkout> to gate a branch
```

What the tests cannot see: they mock `shutil.which` and the extras probe, so
nothing in pytest proves that a *really* bare install still reports `missing`
and still blocks on a missing `ffmpeg`. The gate does that in a clean
`python:3.12-slim` container — core install only, then again with a real static
`ffmpeg` — and asserts AC4/AC5 of the install contract on both sides, plus that
`warn` and `missing` are distinguishable in one `--json` payload. Blocks A and B
expect opposite exit codes in one run, so the gate cannot pass vacuously; it
exits non-zero and names the failing expectation. CI wiring is deliberately not
part of it: the script is the reproducible half, the scheduler is a separate
decision.

### PyPI name pre-flight

```bash
python scripts/check-pypi-name.py                      # the reserved name
python scripts/check-pypi-name.py --explain yt-tools    # just the folding
```

The distribution name is **`yt-tools-cli`**, not `yt-tools`: PyPI refuses a new
project whose name is *too similar* to an existing one, comparing a lossy folding
of the name (`. _ -` dropped, `l|i`→`1`, `o`→`0`). `yt-tools` folds to `ytt001s`,
exactly like the existing YouTube toolkit `yttools`, so it is unregisterable —
and a 404 on the JSON API says only "no such project", never "available". The
script reproduces warehouse's rule against the live index (890k+ names) and exits
non-zero with the conflicting project named. Nothing in the package changes: the
import name stays `yt_tools`, the commands stay `yt-*`, the repo and the plugin
stay `yt-tools`.

## License

MIT — see [LICENSE](LICENSE).
