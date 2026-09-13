# Changelog

All notable changes to yt-tools. Format follows [Keep a Changelog](https://keepachangelog.com/);
the project uses [Semantic Versioning](https://semver.org/).

The version lives in exactly one place — `pyproject.toml` — and is propagated to
`.claude-plugin/plugin.json` and the skill frontmatter by `scripts/sync-version.py`.
Releases before 0.8.0 are summarized only in the git log.

## [Unreleased]

_(nothing yet)_

## [0.23.0] — 2026-09-13

### Changed

- **The distribution is `yt-tools-cli`.** PyPI refuses `yt-tools`: the name folds
  into the same similarity class as the existing `yttools`, so the upload is
  rejected at registration. The project, repository, plugin and every console
  command stay `yt-tools`, and the import name stays `yt_tools` — only what
  `pip`/`pipx` resolve changed.
- Every install hint now names the distribution (`pipx inject yt-tools-cli …`,
  `pip install 'yt-tools-cli[extra]'`), and `doctor` builds its `next_step` from
  the same constant as the refusals instead of a second hardcoded copy that would
  have drifted out of sync.

### Added

- `scripts/publish-check.py`: verifies that a release actually landed on PyPI.
  "Never landed", "version drift" and "incomplete artifacts" are reported as
  three different failures with three different fixes.
- `.github/workflows/release.yml`: tag → build → PyPI by trusted publishing (OIDC,
  no token on disk), with `publish-check` as the final job — a green build is not
  a published release.

## [0.22.1] — 2026-09-13

### Fixed

- `scripts/check-pypi-name.py`: documented the staleness of its own input. The
  names come from the simple index, which is CDN-cached, so a project created
  minutes ago can still read as "available" — the verdict is a pre-flight, not a
  report on the last hour. Found live, on the day `yt-tools-cli` was registered.

## [0.21.0] — 2026-09-13

### Added

- PyPI name pre-flight (`scripts/check-pypi-name.py`) reproducing warehouse's
  `ultranormalize_name` similarity rule against the live index.

## [0.20.0] — 2026-09-13

### Added

- `scripts/check-core-install.sh` — the install contract (AC4/AC5) as a runnable
  gate for a clean `python:3.12-slim`: core install only, then again with a real
  static `ffmpeg`; asserts `can_proceed`/`next_step`/exit code on both sides and
  that `warn` and `missing` stay distinguishable in one `--json` payload. Blocks A
  and B expect opposite exit codes in one run, so it cannot pass vacuously.

## [0.19.1] — 2026-09-13

### Fixed

- `yt-tools cache list` / `cache prune` on an unreadable cache: named cause and a
  non-zero exit instead of a traceback (the helper stays strict; the CLI explains).
- `yt-tools cache list` no longer presents a lower-bound size as an exact total —
  both the entry and the total line carry the qualifier.
- `yt-tools doctor`: a dangling symlink in place of `yt-cache` (or of one of its
  ancestors) is a `warn` with the reason, not an `ok`.

## [0.19.0] — 2026-09-13

### Added

- Public version reader: `yt_tools.__version__` is read from the installed
  distribution metadata (falling back to `pyproject.toml` in a checkout), resolved
  lazily — the last hand-maintained version source is gone.

### Fixed

- Coverage gaps found in review around the version reader.

## [0.18.2] — 2026-09-13

### Fixed

- `doctor` and `cache list` survive paths that are not representable in a cp1251
  console (the Windows default) — output paths are data, not decoration.

## [0.18.1] — 2026-09-13

### Fixed

- `doctor`: an unreadable cache is a `warn` with the reason instead of a crash, and
  a size that could only be partially counted says so ("lower bound").
- `doctor --base` pointing at a file (or at a path whose ancestor is a file) is a
  `warn`, not a silent `ok`.
- Writability is decided by an actual write probe, not `os.access` (which reads the
  DOS read-only attribute on Windows and made the check dead on the primary platform).

## [0.18.0] — 2026-09-13

### Added

- Portable skill: harness-neutral install and usage instructions, plus the `doctor`
  preflight as the documented first step.

## [0.17.0] — 2026-09-12

### Added

- `yt-tools doctor` (`--json`): environment preflight with per-check `status`,
  report-level `can_proceed` and `next_step`. `warn` never blocks; a missing
  required binary (`yt-dlp`, `ffmpeg`) does. Nothing is installed or mutated.

## [0.16.0] — 2026-09-12

### Added

- Single source of version truth: `pyproject.toml`, with `scripts/sync-version.py`
  writing the derived manifests and a guard test failing on divergence.

## [0.15.1] — 2026-09-12

### Fixed

- Extra detection by import probe rather than a package-metadata guess; degradation
  announced with its own wording instead of a refusal.

## [0.15.0] — 2026-09-12

### Changed

- A flow whose optional stack is missing refuses with the exact install command
  instead of raising `ImportError`.

## [0.14.0] — 2026-09-12

### Changed

- Light core: `pip install yt-tools` pulls `youtube-transcript-api` and `yt-dlp`
  only. The heavy stacks moved into `[frames]`, `[audio]` and `[ocr]` extras.

## [0.13.1] — 2026-09-12

### Fixed

- Findings from the umbrella review of the frame-budget cluster.

## [0.13.0] — 2026-09-12

### Added

- A video without subtitles refuses with a named reason instead of an empty file;
  `yt-watch` gained a frames-only mode.

## [0.12.0] — 2026-09-12

### Added

- Frame budget recorded in the `watch.md` artifact.

## [0.11.0] — 2026-09-11

### Fixed

- Unique frame filenames within the same second.

## [0.10.0] — 2026-09-11

### Added

- Near-duplicate frame dedup: the budget is spent on distinct frames.

## [0.9.0] — 2026-09-11

### Added

- Uniform-sampling fallback when scene detection finds nothing (a static video no
  longer yields a single frame).

## [0.8.0] — 2026-09-11

### Added

- `--max-frames` ceiling: a video cannot hand back an arbitrary number of frames.
