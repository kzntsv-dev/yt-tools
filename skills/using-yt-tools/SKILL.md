---
name: using-yt-tools
version: 0.25.1
description: Seven YouTube flows, one skill. Discovery `yt-search` ("find a video about X", «найди видео про X», «поищи туториал»). Summary / exploration `yt-transcript` — [mm:ss] anchors, then pick moments and run `yt-frames` ("what's in this video", «о чём видео»). Targeted frames directly by timestamp ("show frame at N", «покажи кадр на N»). Audio FFT `yt-listen` — BPM, key, chords, structure ("what's the BPM", «спектрограмма», «тональность видео»). Metadata `yt-meta` — description, chapters, most-replayed ("video description", «что в описании», «покажи главы»). Comments `yt-comments` ("top comments", «комменты под роликом»). OCR over cached frames `yt-ocr` — silent-with-text fallback ("read text from frames", "captions burned in", «прочти текст с кадров»). Any youtube.com URL. Install once per machine with pipx (`pipx install 'yt-tools-cli[full]'`); `yt-tools doctor` is the read-only preflight that names what is missing. YouTube-only — Vimeo / Twitch / local files need other tools.
---

# using-yt-tools

Iterative YouTube watching for an agent — clean-markdown transcript with
`[mm:ss]` anchors → the agent decides which moments are interesting →
targeted frame extraction at those timestamps → the agent reads the frames
via its vision tool. An alternative flow — if the user has already named
the timestamps, go straight to frames without the transcript. For musical
URLs there is a third flow with FFT analysis (BPM, key, chord progression,
spectrum) via `yt-listen`.

## When to use

Seven distinct flows, picked by user intent:

**Flow 0 — discovery-search** (find videos by query):

- The user has **no URL yet** — they want to find videos about a topic
  (tutorial, review, lecture, news segment).
- Steps: `yt-search "<query>" --max N` → read the result file → pick a URL
  → continue into Flow A/B/C/D as appropriate.
- Trigger phrases: "find a video about X", "search youtube for X", "find
  tutorial about X", "youtube videos on X", «найди видео про X», «поищи
  туториал по X», «обзор на X youtube», «есть на youtube видео про X».
- This is an **entry-point flow**, not a standalone answer — its output is
  always input to another flow. If the user asks "find me a Maths tutorial
  and tell me what's in it" → Flow 0 → Flow A on the chosen URL.

**Flow A — iterative-watch** (exploration / summary):

- The user asks what's in the video, wants a summary, wants to know what the
  video is about.
- Steps: fetch transcript → read → pick interesting moments → extract those
  frames → Read frames.
- Trigger phrases: "what's in this video", "video summary", "youtube
  transcript", "watch this video", «что в этом ролике», «о чём ролик»,
  «расшифровка YouTube».

**Flow B — targeted-frames** (specific moments):

- The user has already named concrete timestamps; a transcript is extra
  work.
- Steps: extract frames at the given timestamps → Read frames.
- Trigger phrases: "show frame at N", "look at moment N", "what's shown at
  N", «покажи кадр на N», «посмотри момент N», «что показано на N».

**Flow C — audio-analysis** (music FFT):

- The user asks for a music breakdown — BPM, key, harmony, chord
  progression, spectrum, harmonic content.
- Steps: `yt-listen URL --timestamps T1,T2,...` → per timestamp three
  artefacts (`clip.wav` + `spectrum.png` + `features.md`) → Read **both**
  (the PNG via vision + the .md for the numbers).
- Trigger phrases: "listen to fragment at N", "what's the BPM", "harmony",
  "analyze audio", «послушай момент N в <URL>», «какой BPM», «тональность
  видео», «гармония», «спектрограмма», «что в музыке на T».
- If the video has captions, `yt-transcript` is optional context — but
  **not** for lyrics-from-music (see What NOT to do).

**Flow D — metadata** (`yt-meta`, zero added cost):

- The user asks about the description, chapters, the most-replayed parts,
  view/like/comment counts, tags, or which subtitle languages exist.
- Steps: `yt-meta URL` → read `meta.md` → answer. Chapter and most-replayed
  anchors are `[mm:ss]`, so they feed straight into Flow B/C if the user
  then wants frames or audio at those points.
- Free: reuses the same `yt-dlp --dump-json` the transcript path already
  runs — no extra download, no ffmpeg.
- Trigger phrases: "video description", "show chapters", "what are the
  chapters", "most replayed", "video stats", "how many views/likes", «что в
  описании», «покажи главы», «какие главы», «самые пересматриваемые
  моменты», «статистика ролика», «сколько просмотров».

**Flow E — comments** (`yt-comments`, separate paginated scrape):

- The user explicitly wants the comments — top comments, replies, what
  people are saying.
- Steps: `yt-comments URL [--max N] [--sort new]` → read `comments.md` →
  answer.
- **Cost — read this before invoking.** Comments are **not** in the
  metadata dump; they are a separate paginated scrape that can take minutes
  on viral videos. Capped at the **top 50 by default**; raise only with
  `--max` when the user actually needs more. Invoke this flow **only on an
  explicit comments request** — never as part of Flow D, never "while we're
  at it".
- Trigger phrases: "top comments", "what are people saying", "comments
  under the video", "read the comments", «комменты под роликом», «топ
  комментариев», «что пишут в комментах», «почитай комментарии».

**Flow F — OCR** (`yt-ocr`, silent-with-text fallback):

- `yt-transcript` returned nothing (or suspiciously little), or the
  video is from a silent-instructional / burned-in-captions class —
  schematic walkthroughs, chord-matrix tutorials, parameter overlays
  over dimmed B-roll, language without auto-sub but subtitles burned
  into the video frames.
- Steps: `yt-frames URL --mode scene --scene-threshold 12` (lower
  threshold than the default 27 — overlay fades within the same shot
  don't trigger scene-change on the default) **or** `yt-frames URL
  --mode interval --interval 15s` for a denser sample → `yt-ocr URL`
  → Read `<vid>/ocr.md` as a transcript substitute (per-timestamp
  `[mm:ss]` blocks, same paradigm) → continue into Flow B at the
  interesting timestamps if you need the original frames.
- **Requires the `[ocr]` extra** — `rapidocr` + `onnxruntime`, **not** part of
  `[full]`. If missing on the running yt-tools install, `yt-ocr` exits nonzero
  with both install hints (`pipx inject yt-tools-cli "rapidocr>=3.8,<4"
  "onnxruntime>=1.18"` / `pip install 'yt-tools-cli[ocr]'`). The plugin's
  SessionStart hook installs `[full]` (core + `[frames]` + `[audio]`, plus a
  best-effort `bpm-detector` inject) only, **not** `[ocr]` — first run of Flow F
  on a machine may need the inject step. `yt-ocr` reads
  `./yt-cache/<vid>/frames/`, so either run `yt-frames` first or pass
  `--timestamps` (which extracts for you).
- Trigger phrases: "read text from frames", "OCR this video", "what
  does the caption say", "extract overlay text", "captions burned in",
  "silent video with text on screen", «прочти текст с кадров», «OCR
  этого видео», «извлеки текст с кадров», «текст на экране в видео»,
  «silent video с текстом».

All seven flows assume the `yt-tools` CLI is installed — once per machine,
with pipx (see Prerequisites → Installing the CLI). If a host hook is
available it may do that install for you (the bundled Claude Code plugin
does), but no flow depends on one. Binaries may or may not be on the current
session's PATH — that's normal, especially right after a fresh `winget install`
or `pipx ensurepath` (PATH is per-shell, not picked up by the *current* shell).
**Never abort on a bare `Get-Command yt-frames` / `command -v yt-frames`
miss** — first run the resolve chain (see Prerequisites → Locating
binaries).

## Prerequisites

### Installing the CLI

Install once per machine — one command, the same in every harness. The
**distribution** name is `yt-tools-cli` (the project, repo, plugin and all
commands stay `yt-tools`; the import name is `yt_tools`):

```bash
# from PyPI — [full] = core + [frames] + [audio]. NOT OCR (see below).
pipx install "yt-tools-cli[full]"

# core only (transcript / meta / comments / search / cache)
pipx install yt-tools-cli

# reproducible: pin the exact version
pipx install "yt-tools-cli[full]==0.24.2"

# OCR (explicit extra, ~10 MB model fetched on first run). Naming it in the
# install lets pip resolve it from the metadata, bounds included:
pipx install "yt-tools-cli[full,ocr]"
# already installed? widen in place — the bound is `>=3.8,<4` (a major is where a
# params-API change would land; quote the specs — `>`/`<` are redirections in
# bash, cmd.exe and PowerShell):
pipx inject yt-tools-cli "rapidocr>=3.8,<4" "onnxruntime>=1.18"
```

From a checkout instead of PyPI (the plugin path, or tracking `master`):

```bash
pipx install 'git+https://github.com/kzntsv-dev/yt-tools.git#egg=yt-tools-cli[full]'
```

The `[full]` extra is core + `[frames]` + `[audio]` — a flow default, not
"everything": OCR and `bpm-detector` are both outside it. Chord progression,
structural segments and the refined BPM/key in `yt-listen` come from
`bpm-detector`, which is **not on PyPI** and therefore cannot live in the
published metadata — the Python package index rejects direct VCS references.
Add it on top:

```bash
pipx inject yt-tools-cli "bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0"
```

Without it `yt-listen` still runs (librosa-only BPM/key, unfilled sections marked
`n/a`).

Some harnesses do this for you: if your host ships the bundled yt-tools plugin
(Claude Code does), its `SessionStart` hook pipx-installs the `[full]` set from
the plugin's own clone on the first session after install, then injects
`bpm-detector` best-effort — a blocked VCS fetch warns and leaves the
librosa-only path, it never fails the install. A hook is a convenience, not a
requirement: the commands above are the contract.

### First run on a machine: `yt-tools doctor`

Before the first flow — and whenever a flow failed for an unclear reason — run
the preflight. It is read-only (installs nothing, creates nothing) and answers
"what is present, what is missing, and what exactly to run next":

```bash
yt-tools doctor           # report for a human
yt-tools doctor --json    # the same report for a machine
```

Every check carries `status` = `ok` | `warn` | `missing`; the report carries
`can_proceed` and `next_step` (an exact command). `warn` is a recommendation —
an unsupported interpreter, an absent `[audio]` — and only a missing external
binary (`yt-dlp`, `ffmpeg`) blocks. Exit code: `0` when `can_proceed` is true,
`1` when a blocker stands; `--json` never changes it. Act on `next_step`
instead of guessing: it names the per-OS ffmpeg command, the `pipx inject` line
for a missing extra, or the release-drift fix.

External binary that is **not** pip-installable either way:

- **ffmpeg** — required for source video caching and per-clip extraction.
  Per-OS install: `winget install Gyan.FFmpeg` (Windows), `brew install
  ffmpeg` (macOS), `sudo apt install ffmpeg` (Debian/Ubuntu),
  `sudo dnf install ffmpeg` (Fedora/RHEL).

### Locating binaries

This skill makes no assumption about an active PATH. **Step 0 of every
flow** is to resolve the paths for `yt-frames` / `yt-transcript` /
`yt-listen` and `ffmpeg` (`yt-dlp` ships in the same pipx venv). If you
resolve through a fallback path, use PATH-prepend on each invocation (see
Invoke pattern below). Abort **only** if the binary is not on PATH and
not in any of the known install locations.

**yt-tools CLI** (any single location gives all nine binaries —
`yt-search`, `yt-frames`, `yt-transcript`, `yt-meta`, `yt-comments`,
`yt-listen`, `yt-watch`, `yt-ocr`, `yt-tools` — plus a shimmed
`yt-dlp`; `yt-ocr` is the entry-point but the engine itself comes from
the optional `[ocr]` extra):

1. **PATH**: `Get-Command yt-frames` (pwsh) / `command -v yt-frames`
   (bash). For Flow C also probe `yt-listen` (present from pyproject
   `0.2.0+`; if only `yt-frames` is found and `yt-listen` is missing, the
   machine is on an old `0.1.x` install — run `pipx reinstall yt-tools`
   or `pipx upgrade yt-tools`).
2. **pipx-shim direct path** (when PATH is stale after a fresh `pipx
   ensurepath`):
   - Windows: `~/.local/bin/yt-frames.exe` (plus `yt-listen.exe`)
   - Linux/macOS: `~/.local/bin/yt-frames` (plus `yt-listen`)
3. **If neither location resolves** — install hint, then stop. **Do not
   recreate any legacy `.venv/`** even if you find one on disk: the
   current install path is pipx-only, and a stale project-local venv is
   leftover from a pre-distribution layout and should not be used or
   regenerated. Install hint:
   ```
   python -m pip install --user pipx
   python -m pipx ensurepath                                       # one-time; restart shell after
   pipx install "yt-tools-cli[full]"                               # core + frames + audio
   pipx inject yt-tools-cli "bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0"
   # or from a checkout:
   pipx install 'git+https://github.com/kzntsv-dev/yt-tools.git#egg=yt-tools-cli[full]'
   ```
   If your host ships the bundled plugin (Claude Code users: install
   `yt-tools@kzntsv-dev-claude-plugins`), prefer it — its SessionStart hook
   does the pipx install from the plugin's local clone automatically.

**ffmpeg**:

1. PATH: `Get-Command ffmpeg` / `command -v ffmpeg`.
2. Windows winget cache (the path version floats, so glob it):
   `~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_*/ffmpeg-*-full_build/bin/ffmpeg.exe`
3. macOS Homebrew: `/opt/homebrew/bin/ffmpeg` (Apple Silicon) or
   `/usr/local/bin/ffmpeg` (Intel).
4. Linux: `/usr/bin/ffmpeg` (apt) or `/usr/local/bin/ffmpeg`.
5. If no location resolves — print the per-OS install hint
   (`winget install Gyan.FFmpeg` / `brew install ffmpeg` / `apt install
   ffmpeg`) and stop. **Do not** ask the user to restart their host —
   continue the resolve logic in the same session once ffmpeg is
   installed, or note that the next session picks it up on a PATH
   refresh. `yt-tools doctor` prints the same per-OS command.

### Invoke pattern

`yt-frames` spawns `yt-dlp` and `ffmpeg` as child processes via
`subprocess.run([..., "ffmpeg", ...])`, so a full path to `yt-frames.exe`
alone is **not enough** — you need a PATH-prepend so the child processes
also see them.

Substitute `$YTBIN` with the directory where you resolved `yt-frames` in
the probe step (typically `~/.local/bin/` for the pipx-shim layout).

```bash
# bash / git-bash — after resolving through a fallback
FFDIR=$(dirname "$(ls ~/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*-full_build/bin/ffmpeg.exe 2>/dev/null | head -1)")
YTBIN=~/.local/bin
PATH="$FFDIR:$YTBIN:$PATH" yt-frames <url> --timestamps 1:23,4:56
```

```powershell
# pwsh — glob over the floating ffmpeg version
$ff = (Get-ChildItem "$HOME\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*-full_build\bin\ffmpeg.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).DirectoryName
$ytbin = "$HOME\.local\bin"
$env:PATH = "$ff;$ytbin;" + $env:PATH
yt-frames <url> --timestamps 1:23,4:56
```

If both resolved directly on PATH (`Get-Command` returned something), no
prepend is needed — invoke normally.

## Inputs

| Flow | Required | Optional |
|---|---|---|
| 0 — discovery-search | Free-text query | `--max N` (default 10); `--min-duration MM:SS` (skip shorts); `--max-duration MM:SS`; `--out PATH` |
| A — iterative-watch | YouTube URL or bare 11-char video id | `--lang ru,en` for non-English subs; `--out PATH` |
| B — targeted-frames | YouTube URL + timestamps (`mm:ss`, `h:mm:ss`, or bare seconds: `123` → 2:03) | `--no-cache-source` (stream instead of caching `source.mp4`); `--out DIR` |
| C — audio-analysis | YouTube URL + timestamps (same formats as B) | `--duration 30s` (default 30s — the lower bound for beat-tracking); `--mode interval --interval 60s` (bulk sampling); `--no-wav` / `--no-spectrogram` (both default ON); `--linear` (STFT instead of mel); `--chroma` (bonus chromagram PNG); `--sample-rate 22050`; `--no-cache-source`; `--out DIR` |
| D — metadata | YouTube URL or bare 11-char video id | `--out PATH` |
| E — comments | YouTube URL or bare 11-char video id | `--max N` (default 50; higher = slower); `--sort {top,new}` (default top); `--out PATH` |
| F — OCR | YouTube URL or bare 11-char video id (frames in cache, or pass `--timestamps`) | `--timestamps T1,T2,…` (extract via internal yt-frames then OCR); `--language {en,ru,ja,zh,multi}` (default `en`; `ja` is read by the PP-OCRv4 recognizer — no PP-OCRv5 Japanese model exists); `--out PATH` |

**Frame budget.** Auto-selected frames (`--mode scene`, `--mode interval`, and
`yt-watch`) are capped at **100 per call** and evenly thinned with the first and
last frame always kept — a cut-heavy clip can't hand you hundreds of frames, and
a long clip keeps its tail. `--max-frames N` changes the budget, `--max-frames 0`
disables it, and an explicit `--timestamps` list is **never** capped or thinned
(your own choice stays yours). Thinning is reported on stderr. `yt-watch` obeys
the same budget for the frames it embeds, with the same `--max-frames` /
`--no-dedup` flags; its `watch.md` links **only** the survivors (dropped
candidates are deleted, so every link is a readable file) and `yt-watch` stdout
stays one line — the path of `watch.md`.

A static clip (talking head, screencast, one long take) has almost no cuts, so
scene detection alone would hand you **one frame for the whole video**. When
fewer than **8** scenes are detected, the same budget goes into an even scan of
the timeline instead, announced on stderr — you always know whether you are
looking at scenes or at a uniform scan.

Near-duplicates (the same slide held on screen) are dropped **before** the cap, so
the budget buys frames that differ instead of copies: 16×16 grayscale thumbnail,
mean-abs-diff against the last kept frame, threshold 2.0/255, fail-open. `--no-dedup`
turns it off. Frames you never get are not printed as `Wrote:` — stdout lists only
what is on disk and readable.

Flows A–E write to `<cwd>/yt-cache/<video-id>/` by default (Flow C into
the `audio/` sub-directory; Flow F also writes under
`<video-id>/` as `ocr.md`). **Flow 0** writes to
`<cwd>/yt-cache/_search/` instead — its artefacts aren't tied to a single
video. Flows 0, D, and E need only the `yt-search` / `yt-meta` /
`yt-comments` binary plus the shimmed `yt-dlp` (same pipx venv) — **no
ffmpeg**, no `source.mp4` download. Flow F **batch-on-cache** mode
(default) needs neither yt-dlp nor ffmpeg — it reads what `yt-frames`
already wrote; only the `--timestamps` mode triggers extraction (yt-dlp
+ ffmpeg + `source.mp4` cache).

## Steps

### Flow 0 — discovery-search

```
0. Resolve yt-search per Prerequisites → Locating binaries; build PATH-prepend ($YTBIN only — no ffmpeg) if resolved via fallback
1. yt-search "<query>" [--max N] [--min-duration M:SS]  → ./yt-cache/_search/<slug>-<unix-ns>.md
2. Read the result file
3. Pick a URL (it's the last field of each block — `grep -oP 'https://[^\s]+'` works too if you want the raw list)
4. Continue: Flow A (transcript / summary) or Flow B (specific frames) or Flow D (metadata) on the chosen URL
```

Single stdout line: the bare absolute path of the search artefact. The
filename embeds a nanosecond-resolution unix timestamp
(`<slug>-<unix-ns>.md`), so re-running the same query never overwrites a
previous result — even sub-second re-runs land in distinct files.
`--min-duration` / `--max-duration` require an explicit `MM:SS` (or
`H:MM:SS`) — a bare integer is rejected to avoid the seconds-vs-minutes
ambiguity. The
underlying `yt-dlp ytsearch` ranking is YouTube-defined and **not stable**
between calls — re-running may reshuffle the top result; treat the file
as a snapshot, not a cache.

### Flow A — iterative-watch

```
0. Resolve yt-frames + ffmpeg per Prerequisites → Locating binaries; build PATH-prepend if resolved via fallback
1. yt-transcript <url>                        → ./yt-cache/<vid>/transcript.md
2. Read transcript.md, find [mm:ss] anchors that match the question
3. yt-frames <url> --timestamps 1:23,4:56,…   → ./yt-cache/<vid>/frames/frame_*.jpg
4. Read each frame_*.jpg with the host's image reading (vision tool /
   vision-capable model)
5. Answer the user, citing both transcript paragraph and frame contents
```

**Stdout contract per CLI** (take the last line; format depends on the
CLI):

- `yt-transcript`, `yt-watch` — one stdout line: the bare absolute path of
  the artefact (`<abs>/transcript.md` or `<abs>/watch.md`).
- `yt-frames` — **N lines** of the form `Wrote: <abs path>`, one per
  extracted frame. Strip the `"Wrote: "` prefix to get each path. This is
  deliberate per-line output so callers can pipe / scrape without parsing
  a summary line.

Warnings and errors go to stderr (`warning: …`, `error: …`); stdout stays
machine-parseable.

#### Transcript-cue frames — your judgment, not a regex

Scene- and interval-selection both miss the moments a presenter *points at*
something on screen. Pointing at a slide is a **low** visual change, so the
scene detector has nothing to fire on ("look here", "as you can see",
"notice this", «вот здесь», «посмотрите», «обратите внимание»).

Those moments are yours to pick, by reading the transcript. This is a
judgment call — rhetorical uses ("look, the point is…") are not cues — so
it is deliberately **not** a regex, and no tool does it for you.

Two-pass recipe, on top of Flow A:

```
1. yt-transcript <url>                        → ./yt-cache/<vid>/transcript.md   (Flow A step 1)
2. Read transcript.md and pick the deictic moments yourself
3. yt-frames <url> --timestamps 4:32,7:10,9:55  → extra frames at exactly those moments
4. Read those frame_*.jpg alongside the scene frames you already have
```

The three cues that most often carry a payload, and the ones context says to
skip:

| Pick a frame when the speaker… | Skip when… |
|---|---|
| «look at this / here / at the top-right» | «look, the thing is…» (rhetorical filler) |
| «as you can see / notice / watch what happens» | «as I said earlier» (back-reference, nothing new on screen) |
| «this number / this chart / this graph» | «this is important» (no referent) |

Cost: near zero. Step 1 already downloaded `source.mp4`, so the second call
is a local `ffmpeg -ss`, no re-download and no re-transcription.

Boundary — the second call is **standalone, not additive**: `--timestamps`
replaces the selection mode and returns only the frames you asked for. It is
not merged with whatever `--mode scene` produced. If you want both sets, keep
both directories (or pass `--out` on one of them) — nothing merges them for
you.

### Flow B — targeted-frames

```
0. Resolve yt-frames + ffmpeg per Prerequisites → Locating binaries; build PATH-prepend if resolved via fallback
1. Parse the user's timestamps (mm:ss / h:mm:ss / bare seconds — all accepted)
2. yt-frames <url> --timestamps 1:23,4:56,…   → ./yt-cache/<vid>/frames/frame_*.jpg
3. Read each frame_*.jpg
4. Answer the user, referencing each frame by its [mm:ss] label
```

No transcript fetch. If the user later asks "what was said at that
moment?", switch to Flow A on the same URL — the `source.mp4` cache is
reused, so there is no re-download.

### Flow C — audio-analysis

```
0. Resolve yt-listen + ffmpeg per Prerequisites → Locating binaries; build PATH-prepend if resolved via fallback
1. Parse timestamps (mm:ss / h:mm:ss / bare seconds — same as Flow B)
2. yt-listen <url> --timestamps T1,T2,...    → ./yt-cache/<vid>/audio/{clip,spectrum,features}_TTTT.{wav,png,md}
3. Read **both** per timestamp: features_TTTT.md (numbers — BPM, key, chord progression, spectral features, peak frequencies, harmonic/percussive split) + spectrum_TTTT.png (vision)
4. Reason about BPM / key / chord / spectral. Cite the concrete numbers from features.md; the spectrum PNG is a supplementary signal, not the primary one (see What NOT to do).
```

The `yt-listen` stdout contract is one `Wrote: <abs path>` line per
artefact (three per timestamp — wav, png, md), same as `yt-frames`.

The `source.mp4` cache is shared between Flows A / B / C on the same URL
— no repeated downloads. Default duration is 30s (the lower bound for
beat-tracking); `--duration` overrides it. For bulk-sampling a musical
video, use `--mode interval --interval 60s` instead of explicit
timestamps.

### Flow D — metadata

```
0. Resolve yt-meta per Prerequisites → Locating binaries; build PATH-prepend ($YTBIN only — no ffmpeg) if resolved via fallback
1. yt-meta <url>                              → ./yt-cache/<vid>/meta.md
2. Read meta.md
3. Answer; if chapters / most-replayed anchors are relevant, offer Flow B/C at those [mm:ss]
```

Single stdout line: the bare absolute path of `meta.md`. Warnings/errors to
stderr. Free — no `source.mp4`, no ffmpeg; only the `yt-dlp --dump-json`
the engine already runs.

### Flow E — comments

```
0. Resolve yt-comments per Prerequisites → Locating binaries; build PATH-prepend ($YTBIN only — no ffmpeg) if resolved via fallback
1. yt-comments <url> [--max N] [--sort new]   → ./yt-cache/<vid>/comments.md   (top 50 by default)
2. Read comments.md
3. Answer, citing authors / like-counts; replies are nested as blockquotes under their parent
```

Single stdout line: the bare absolute path of `comments.md`. **Costly** —
a separate paginated scrape (minutes on viral videos), so the fetch is
capped at top 50 unless the user asked for more via `--max`. Only run this
flow on an explicit comments request (see What NOT to do).

### Flow F — OCR (silent-with-text fallback)

```
0. Resolve yt-frames + ffmpeg + yt-ocr per Prerequisites → Locating binaries; build PATH-prepend ($YTBIN + $FFDIR) if resolved via fallback
1. Populate the frames cache:
     yt-frames <url> --mode scene --scene-threshold 12     # lower than the default 27 — overlay fades within the same shot don't trigger scene-change on default
     # or, if scene-detect misses too much:
     yt-frames <url> --mode interval --interval 15s
2. yt-ocr <url> [--language {en,ru,ja,zh,multi}]            → ./yt-cache/<vid>/ocr.md
3. Read ocr.md — per-timestamp [mm:ss] blocks, same paradigm as transcript.md;  _(no text detected)_  marker means the frame was checked and produced nothing (vs. silently omitted)
4. Treat ocr.md as a transcript substitute: pick the interesting [mm:ss] anchors; continue into Flow B for the original frames if needed
```

Or skip step 1 entirely and let yt-ocr do extraction on its own:

```
yt-ocr <url> --timestamps 1:30,2:45,5:10           # standalone — extracts via internal yt-frames helper, then OCR
```

Single stdout line: the bare absolute path of `ocr.md`. Heads-up that
Flow F **batch-on-cache** mode (no `--timestamps`) requires existing
frames — if `./yt-cache/<vid>/frames/` is empty or missing, `yt-ocr`
exits nonzero with a hint to either run `yt-frames` first or pass
`--timestamps`. The `[ocr]` extra (`rapidocr` + `onnxruntime`) is **not**
installed by the SessionStart hook — first run typically needs `pipx
inject yt-tools-cli rapidocr onnxruntime` on its own; the CLI surfaces the
exact command in its error message when the extra is missing.

## Failure modes

All failures abort cleanly; never leave a half-finished state.

| Symptom | Cause | Action |
|---|---|---|
| Anything about the environment is unclear (is `ffmpeg` there? which extras are installed?) | — | `yt-tools doctor --json` — read per-check `status`, `can_proceed`, `next_step`; run `next_step` |
| `yt-transcript` / `yt-frames` not on PATH | yt-tools is installed but the session's PATH does not include the pipx-shim directory | Run the **full** probe chain (PATH → `~/.local/bin/`). Abort and print the install hint **only** if neither location yielded anything. **Do not** reinstall yt-tools when a pipx-shim exists — it's a PATH problem, not a missing package (see What NOT to do). |
| Flow 0 — `_No results._` body in the search file | YouTube returned zero matches for the query | Reformulate (broaden / drop modifiers / drop non-ASCII) and re-run. Do **not** flip to a different engine — Phase 1 is yt-dlp-only by design. |
| Flow 0 — every result filtered out by `--min-duration` / `--max-duration` | The post-filter is too tight (e.g. `--min-duration 30:00` on a topic dominated by 5-minute reviews) | Relax the filter and re-run. Results without a numeric `duration` (live streams, some shorts) are dropped by `--min-duration` because we can't prove they meet it. |
| `yt-dlp not found on PATH` (from a child process) | `yt-dlp` lives in the same pipx venv as `yt-frames`, but the PATH-prepend was not built | Re-build the PATH-prepend (Prerequisites → Invoke pattern) — point `$YTBIN` at the directory where you found `yt-frames`. |
| `ffmpeg not found on PATH` (from a child process) | ffmpeg is installed but only in the winget cache / Homebrew prefix / etc., not on the session's PATH | Run the ffmpeg resolve per Prerequisites and PATH-prepend. Abort only if no location yielded the binary — then print the per-OS install hint. **Do not** require the user to restart their host — the resolve handles it, and `yt-tools doctor` names the same command. |
| `yt-dlp source download failed (exit N) \| stderr: …` | Network failure / private / age-gated / region-locked / malformed URL | Print the captured stderr verbatim; do not retry. |
| `yt-dlp --dump-json failed` | Same, but on the metadata step | Same. |
| `Subtitles disabled` / `no captions in the requested language(s)` / `the transcript API returned nothing usable (ParseError: …)` from `yt-transcript` | Captions are absent (channel-disabled, wrong `--lang`, or YouTube answered with an empty body — the last one used to surface as a bare `no element found: line 1, column 0`). The message names the reason and both fallbacks. | Fatal for Flow A — tell the user the reason, and switch: Flow C (`yt-listen`) for audio, Flow F (`yt-ocr`) for text burned into the frames. Try another `--lang` first only when the reason says the *requested language* is missing. `yt-watch` on the same video does **not** fail — it writes a frames-only `watch.md` with the reason in its header, so read the frames and say that the text layer is absent. |
| Flow B/D — `yt-frames --mode scene requires the [frames] extra` / `yt-watch requires the [frames] extra` | `scenedetect` / `opencv-python` not in the active pipx venv | Exit code 1, nothing was downloaded. Tell the user the command the CLI printed (`pipx inject yt-tools-cli scenedetect opencv-python` or `pip install 'yt-tools-cli[frames]'`). Fallbacks that need no install: Flow D with an explicit `--timestamps` list, or `--mode interval` (which still runs and announces on stderr that near-duplicate dedup was skipped). |
| Flow C — `yt-listen requires the [audio] extra` | `librosa` / `matplotlib` not in the active pipx venv | Exit code 1. Same shape: print the command the CLI named. Until it is installed, Flow A (transcript) and Flow F (`yt-ocr`) are the alternatives for this video. |
| Flow F — `yt-ocr requires the [ocr] extra` | `rapidocr` / `onnxruntime` not in the active pipx venv | Run the inject command the CLI printed: `pipx inject yt-tools-cli "rapidocr>=3.8,<4" "onnxruntime>=1.18"` (or `pip install 'yt-tools-cli[ocr]'` in non-pipx setups). The bound is load-bearing: an inject resolves from PyPI directly and never inherits the extra's metadata (`>`/`<` need the quotes). The plugin's SessionStart hook installs `[full]`, not `[ocr]`, so first run typically needs this. |
| Flow F — `no cached frames in …/frames` | Batch-on-cache mode but `yt-frames` wasn't run yet (or wrote elsewhere) | Either run `yt-frames URL --mode scene --scene-threshold 12` (or `--mode interval --interval 15s`) first, or re-invoke `yt-ocr URL --timestamps T1,T2,…` to let it do extraction inline. |
| Flow F — RapidOCR model download fails (offline / firewall) | First-run lazy-download of PP-OCRv5 ONNX weights blocked | Print the captured error; let the user re-run with network access. Don't retry. |
| User passed a non-YouTube URL (Vimeo / Twitch / local mp4) | Out of scope | Stop; say the skill is YouTube-only. |

## Side effects

- Writes under `<cwd>/yt-cache/`:
  - `_search/<slug>-<unix>.md` (Flow 0 — outside any `<video-id>/`)
  - `<video-id>/transcript.md` (Flow A)
  - `<video-id>/meta.md` (Flow D)
  - `<video-id>/comments.md` (Flow E)
  - `<video-id>/ocr.md` (Flow F)
  - `<video-id>/source.mp4` (≤ 720p; produced by Flows A/B/C unless `--no-cache-source`; **not** by 0/D/E; produced by Flow F **only** when `--timestamps` triggers extraction)
  - `<video-id>/frames/frame_<mmss>.jpg` per extracted frame (also re-read by Flow F); a sub-second moment carries its milliseconds (`frame_0130_250.jpg` = 90.25 s), so two frames in one second never overwrite each other and stdout's `Wrote:` count equals the files on disk
  - `<video-id>/audio/{clip,spectrum,features}_<mmss>.{wav,png,md}` (Flow C)
- Flow F also lazy-downloads RapidOCR PP-OCRv5 ONNX weights on first
  invocation with a given `--language` (~8 MB for the english recognizer plus the
  shared detector). In RapidOCR 3.8.x they land in the installed package's own
  `rapidocr/models/` directory — not in `~/.cache/`, which is where this line
  claimed they went until a clean-venv check (2026-09-13).
- Network: `yt-dlp` pulls metadata + optionally `source.mp4`;
  `youtube-transcript-api` pulls subs; Flow E additionally paginates the
  comment feed (the one heavy network path — minutes on viral videos).
- No external state is mutated — pure local-fs side effects.
- `source.mp4` may be ~50–200 MB per 720p / 10-minute video; the cache is
  reused between calls. **Warning** — it accumulates: 20 videos ≈ 1–4 GB
  on disk.

Cache hygiene: `yt-tools cache list` shows usage, `yt-tools cache prune
--older-than 7d` clears the old ones. A size marked `(lower bound)` means part
of that directory could not be read — the real figure is larger. If the cache
root itself is unreadable, both commands name the cause and exit non-zero
instead of printing a traceback.

## What NOT to do

- **Don't recreate a deleted venv from the install hint.** If you find a
  stale `lib/yt-tools/.venv/` somewhere on disk, do not regenerate it —
  the current install path is pipx-only. Check `~/.local/bin/yt-frames`
  first. An empty `.venv/` ≠ "yt-tools is not installed": the machine
  has migrated to pipx and the old venv is legacy. Recreating a venv on
  top of a working pipx install is a destructive cleanup paradox (wastes
  ~200 MB and creates two parallel installs). If the pipx-shim exists but
  `Get-Command yt-frames` is empty, the fix is `pipx ensurepath` +
  restart shell, not a new venv.
- **Don't run Flow 0 when the user already gave you a URL.** A YouTube
  URL in the request — `youtube.com/watch?v=…`, `youtu.be/…`, or a bare
  11-char id — means Flow A/B/C/D/E on that URL, not a fresh search.
  Don't "verify" by running yt-search first; the URL is the user's
  decision, not a hypothesis to confirm.
- **Don't infer answers about the videos directly from the Flow 0
  result file.** It only carries title / channel / duration / views —
  no description, no transcript, no absolute upload date (YouTube only
  exposes relative dates in search results; the `uploaded:` line is
  almost always omitted). To answer "what's the video about?" or "when
  was it uploaded?" pick a URL and continue into Flow A or D. Reasoning
  off the title alone is a hallucination risk.
- **Don't run Flow A when the user has already named timestamps.** "Look
  at 1:23 and 4:56" → go straight to Flow B. Fetching the transcript
  first is pure waste.
- **Don't bulk-extract "just in case".** Flow A takes frames from the
  transcript, Flow B from explicit user input. Never `--mode interval
  --interval 5s` "to be safe".
- **Don't run `yt-comments` unless the user explicitly asked for comments.**
  It is the one costly flow — a paginated scrape, minutes on viral videos —
  and is **not** part of the metadata flow. "What's this video about" → Flow
  A/D, never E. And don't raise `--max` past the default 50 on your own; more
  comments = proportionally slower, so only when the user asks for depth.
- **Don't use `yt-watch` as the default Flow A renderer.** `yt-watch`
  combines transcript + scene-frames into a single document — heavier
  (requires an ffmpeg scene-detect pass over `source.mp4`). Use it only
  when the user wants one self-contained document.
- **Don't shell-quote URLs as a single command string.** Use the CLI
  list-form (already list-form in `subprocess.run`); YouTube URLs contain
  `?` and `&`, which break naive quoting.
- **Don't retry on `yt-dlp` failures.** A failure here means the video is
  genuinely unavailable (private / region / age) or the user's auth is
  broken. Retries waste tokens.
- **Don't paraphrase / translate the transcript silently in your answer.**
  The artefact is for your reasoning; cite it with `[mm:ss]` anchors when
  you quote a passage.
- **Don't invoke the skill on non-YouTube URLs.** Vimeo / Twitch / TikTok
  / local mp4 — out of scope. Use other tools (or `yt-dlp` directly).
- **Don't write results to arbitrary paths.** Default is
  `<cwd>/yt-cache/<vid>/`; pass `--out` only if the user explicitly asked
  for it.
- **Don't run Whisper on mixed music.** If the user asks for lyrics from
  a musical clip — this is **not** `yt-listen`'s job. Whisper on mixed
  music without source separation is garbage (confirmed in arXiv
  2506.15514). Tell the user that lyrics-from-music is a separate
  pipeline (Demucs / Spleeter source-separation + Whisper over the
  isolated vocals), out of scope for the current `yt-tools`. Don't try to
  substitute `yt-transcript` either — YouTube auto-subs for music are
  usually absent, and `yt-listen` has no Whisper flag even as an option.
- **Don't interpret `spectrum_*.png` without `features_*.md` in the same
  pair.** VLM signal on audio spectrograms is limited (~50–60 %
  accuracy on ESC-10 vs 72.5 % human; Dixit et al. arXiv 2411.12058). The
  numeric digest from `features.md` is the primary channel; the spectrum
  PNG is a supplementary visual cue. When you read the PNG, always read
  the `.md` for the same timestamp; cite BPM / key / chord / spectral
  values from the textual fields, not from "what the picture looks
  like".
- **Don't run Flow F on videos where the transcript is already
  informative.** OCR over every cached frame is the more expensive
  fallback — meant for the silent-instructional / burned-in-captions
  class where `yt-transcript` returns nothing useful. If `transcript.md`
  has substantive content, stay on Flow A. Reading the same content
  twice (once from auto-subs, once from OCR over burned-in text) wastes
  tokens.
- **Don't pass `--language auto` to `yt-ocr`.** No auto-detection in
  v1 — supported values are exactly `en` (default) / `ru` / `ja` /
  `zh` / `multi`. If the user doesn't name a language, default to `en`;
  if they say "русский / Japanese / Chinese", pass the matching code
  explicitly. (`multi` is the PP-OCR multilingual model — slower, wider
  CJK + Latin coverage; use it when the video mixes scripts. `en`/`ru`/
  `zh`/`multi` read with a PP-OCRv5 recognizer and `ja` with the v4 one —
  the header line of `ocr.md` names whichever ran.)
- **Don't try to OCR arbitrary `.jpg` files with `yt-ocr`.** This skill
  and CLI live in the YouTube domain: `yt-ocr` only accepts a YouTube
  URL / video-id and reads frames from `./yt-cache/<vid>/frames/`. For
  one-off image OCR, the user wants a general-purpose tool (e.g.
  `rapidocr` directly), not yt-tools.
