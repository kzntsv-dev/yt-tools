"""yt-frames CLI — extract frames at given timestamps, intervals, or scene boundaries.

Three modes (mutually exclusive):
  --timestamps 1:23,4:56     (default if --timestamps is given)
  --mode interval --interval 30s
  --mode scene  --scene-threshold 27

Source-mp4 cache: the first call for a URL downloads ``source.mp4`` (720p) into
``./yt-cache/<vid>/``; subsequent calls reuse the local file via ``ffmpeg -ss``.
Use ``--no-cache-source`` to stream the source via ``yt-dlp -g | ffmpeg`` instead.

Each extracted frame prints ``Wrote: <abs path>`` on its own stdout line so callers
can pipe / scrape without parsing a summary at the end.

Frame budget: auto-selected frames (``--mode scene`` / ``--mode interval``) are held
to ``--max-frames`` (default 100) by even thinning that always keeps the first and
the last frame, so a cut-heavy clip cannot hand the agent hundreds of frames and a
long clip keeps its tail. ``--max-frames 0`` disables the cap explicitly, and an
explicit ``--timestamps`` list is never capped or thinned at all — the caller made
that choice.

Scene fallback: a static clip (talking head, screencast) has almost no cuts, so
``--mode scene`` would hand the agent a single frame for the whole video. When
fewer than ``MIN_SCENE_FRAMES`` scenes are detected, the same budget is spent on an
even scan of the timeline instead, and a warning on stderr says so — the agent must
know it is looking at a uniform scan rather than at scenes. An explicit
``--max-frames 0`` cannot mean "every frame" for such a scan, so it falls back to
``MIN_SCENE_FRAMES`` frames rather than to the whole video.

Dedup: auto-selection pays for the same slide over and over, so candidates that are
near-copies of the last kept frame are dropped before the cap is applied (``cv2``
16×16 grayscale thumbnail, mean-abs-diff against the last *kept* frame, threshold
``DEDUP_THRESHOLD``). Dropping copies first frees budget for frames that actually
differ. Any failure here is fail-open — an unreadable frame is kept, never lost —
and ``--no-dedup`` turns the whole pass off.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from yt_tools._metadata import MetadataError, fetch_video_metadata
from yt_tools.core import (
    cache_dir_for,
    extract_video_id,
    force_utf8_streams,
    format_seconds_for_filename,
    frame_filename,
    interval_timestamps,
    parse_timestamp_to_seconds,
)
from yt_tools.extras import require_extra, warn_missing_extra

SOURCE_FORMAT_SPEC = "bv*[height<=720]+ba/b[height<=720]"
DEFAULT_SCENE_THRESHOLD = 27.0
DEFAULT_MAX_FRAMES = 100
MIN_SCENE_FRAMES = 8
DEDUP_THUMB_SIZE = 16
DEDUP_THRESHOLD = 2.0  # mean-abs-diff on 0..255, against the last kept frame (D8)
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|m|min|h)?$", re.IGNORECASE)


def parse_interval(spec: str) -> float:
    """Parse ``30s`` / ``1m`` / ``2h`` / bare seconds into a float number of seconds."""
    m = _INTERVAL_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid interval: {spec!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "sec"):
        return value
    if unit in ("m", "min"):
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    raise ValueError(f"invalid interval unit: {unit!r}")


def _thin_evenly(items: list[float], limit: int | None) -> list[float]:
    """Keep at most ``limit`` items, evenly spaced, first and last preserved.

    ``limit`` of ``None``, ``0`` or ``>= len(items)`` keeps every item (``0`` is the
    documented "no cap" value). ``limit == 1`` keeps only the first item.

    Thinning is spread over the whole list rather than truncating it: the step is
    ``(n-1)/(limit-1) >= 1`` whenever ``limit <= n``, so kept indices never collide
    and the tail of a long video is never the part that gets dropped.
    """
    n = len(items)
    if limit is None or limit <= 0 or limit >= n:
        return list(items)
    if limit == 1:
        return [items[0]]
    return [items[round(i * (n - 1) / (limit - 1))] for i in range(limit)]


def _uniform_timestamps(duration: float, count: int) -> list[float]:
    """``count`` timestamps spread evenly over ``[0, duration)``, starting at 0.

    The step is ``duration / count``, so the last sample sits one step short of the
    end: seeking exactly to the duration yields no frame at all. It is a mitigation,
    not a guarantee — a container can still report more seconds than it can decode,
    and auto-selection survives that by skipping the unextractable candidate (see
    ``_extract_candidates``).
    """
    if count <= 0 or duration <= 0:
        return []
    return [duration * i / count for i in range(count)]


def _probe_source_duration(source: Path) -> float:
    """Duration of a local video in seconds, or ``0.0`` when the container won't say.

    Uses the already-paid PySceneDetect stack (same opener as scene detection), so
    no new binary or network call is introduced for the fallback.
    """
    from scenedetect import open_video

    try:
        duration = open_video(str(source)).duration
        return float(duration.get_seconds()) if duration is not None else 0.0
    except Exception:  # fail-open: без длительности просто не будет фолбэка (D10 spirit)
        return 0.0


def _scene_timestamps_with_fallback(source: Path, threshold: float, max_frames: int | None) -> list[float]:
    """Scene boundaries, or an even scan of the timeline when there are too few of them.

    Static footage collapses to one scene and would give the agent a single frame for
    the whole video (contract D6). The fallback spends the same budget as the cap: the
    scan is ``max_frames`` wide, or ``MIN_SCENE_FRAMES`` when the cap is off — an
    uncapped *uniform* scan would mean every frame of the video, which is no scan at
    all. A small explicit cap is honoured as given (``--max-frames 1`` gives one
    frame): the caller's own number is not second-guessed (D1/D3). The warning is
    mandatory: a fallback the agent cannot see is a wrong answer it cannot detect.
    """
    scenes = _detect_scene_timestamps(source, threshold)
    if len(scenes) >= MIN_SCENE_FRAMES:
        return scenes
    duration = _probe_source_duration(source)
    if duration <= 0:
        # Фолбэк не состоялся — но молчать нельзя: агент должен знать, что вместо скана
        # по всему ролику ему достались одни сцены, и что ролик, возможно, статичный.
        print(
            f"note: uniform fallback skipped: could not read the duration of {source.name}"
            f" — handing back {len(scenes)} scene frame(s) as-is",
            file=sys.stderr,
        )
        return scenes
    count = max_frames if max_frames and max_frames > 0 else MIN_SCENE_FRAMES
    uniform = _uniform_timestamps(duration, count)
    print(
        f"note: fallback to uniform sampling: only {len(scenes)} scene(s) detected"
        f" (< {MIN_SCENE_FRAMES}) - {len(uniform)} frames over {duration:.0f}s",
        file=sys.stderr,
    )
    return uniform


def _frame_signature(path: Path):
    """16×16 grayscale thumbnail of a frame, or ``None`` when it can't be read (D8)."""
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    return cv2.resize(image, (DEDUP_THUMB_SIZE, DEDUP_THUMB_SIZE), interpolation=cv2.INTER_AREA)


def _dedup_frames(paths: list[Path], threshold: float = DEDUP_THRESHOLD) -> list[Path]:
    """Drop frames that are near-copies of the last *kept* frame (D7/D8).

    Fail-open by design (D10): a frame that cannot be read or compared is kept —
    paying once for a copy is cheaper than losing a real frame. The first candidate is
    always the reference, so it always survives; a copy at the very end of the range is
    dropped, and the range stays covered by the last frame that actually differs (D14).
    """
    if len(paths) <= 1:
        return list(paths)
    try:
        import numpy as np
    except Exception:
        return list(paths)

    kept: list[Path] = []
    reference = None
    for path in paths:
        try:
            signature = _frame_signature(path)
        except Exception:
            signature = None
        if signature is None:
            kept.append(path)  # fail-open: не прочитался — значит остаётся
            continue
        if reference is None:
            kept.append(path)
            reference = signature
            continue
        try:
            # float32: uint8-вычитание завернулось бы через ноль и «нашло» разницу там,
            # где кадры одинаковы.
            difference = float(np.abs(signature.astype(np.float32) - reference.astype(np.float32)).mean())
        except Exception:
            kept.append(path)
            continue
        if difference > threshold:
            kept.append(path)
            reference = signature
    return kept


def _remove_quietly(path: Path) -> None:
    """Delete a candidate the agent will never get; a failure here is not fatal (D10)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _select_frames(written: list[Path], max_frames: int | None, dedup: bool) -> list[Path]:
    """Dedup, then thin to the cap, then announce the numbers on stderr (D11/D12).

    Order matters: dedup first, because it frees budget that the cap then spends on
    frames that differ (D11). A pure selector — it never touches the filesystem, so the
    two callers that own different side effects (``yt-frames`` deletes and prints
    ``Wrote:``, ``yt-watch`` embeds the survivors in markdown) share one arithmetic.
    """
    candidates = list(dict.fromkeys(written))  # один и тот же момент дважды в списке
    # Dedup is a quality step, not a prerequisite: without cv2 the frames are still
    # written, but the drop is announced rather than silent (D4/D6).
    if dedup and warn_missing_extra("near-duplicate dedup", "frames", modules=("cv2",)):
        dedup = False
    kept = _dedup_frames(candidates) if dedup else candidates
    if len(kept) != len(candidates):
        print(
            f"note: dedup dropped {len(candidates) - len(kept)} of {len(candidates)} candidate frame(s)"
            f" (near-duplicates, threshold {DEDUP_THRESHOLD:.1f}/255)",
            file=sys.stderr,
        )
    survivors = _thin_evenly(kept, max_frames)
    if len(survivors) != len(kept):
        print(
            f"note: {len(kept)} candidate frames thinned to {len(survivors)}"
            f" (--max-frames {max_frames}; 0 disables the cap)",
            file=sys.stderr,
        )
    return survivors


def _apply_budget(written: list[Path], max_frames: int | None, dedup: bool) -> list[Path]:
    """Select survivors, delete the rest, and print ``Wrote:`` for what remains (D15).

    Files the agent never gets are removed rather than announced — stdout must not
    announce a frame the agent cannot read.
    """
    survivors = _select_frames(written, max_frames, dedup)
    survivors_set = set(survivors)
    for path in dict.fromkeys(written):
        if path not in survivors_set:
            _remove_quietly(path)
    for path in survivors:
        print(f"Wrote: {path}")
    return survivors


def _extract_candidates(
    url: str,
    seconds_list: list[float],
    out_dir: Path,
    source: Path | None,
    tolerate_failures: bool,
) -> list[Path]:
    """Extract one frame per timestamp, in order. Returns the paths actually written.

    ``tolerate_failures`` is the auto-selection mode: a container can report a duration
    a frame or two longer than the material it holds, so the last sample of an even scan
    can land past the end and yield no frame at all. Losing 99 good frames over one
    bad tail is not acceptable, so such a candidate is skipped and reported — but an
    empty result is still an error, otherwise a broken toolchain would look like an
    empty video. An explicit ``--timestamps`` list stays strict (``source`` is not
    consulted when it is ``None``, which selects the streaming extractor).
    """
    written: list[Path] = []
    failures: list[tuple[float, str]] = []
    for seconds in seconds_list:
        out_path = out_dir / frame_filename(seconds)
        try:
            if source is None:
                _ffmpeg_extract_streaming(url, seconds, out_path)
            else:
                _ffmpeg_extract_from_file(source, seconds, out_path)
        except RuntimeError as e:
            if not tolerate_failures:
                raise
            failures.append((seconds, str(e)))
            continue
        written.append(out_path.resolve())
    if failures:
        print(
            f"note: skipped {len(failures)} candidate frame(s) ffmpeg could not extract"
            f" (first at {failures[0][0]:.3f}s — past the end of the video?)",
            file=sys.stderr,
        )
    if not written and failures:
        raise RuntimeError(f"no frames extracted: {failures[0][1]}")
    return written


def _require_bin(name: str) -> None:
    if not shutil.which(name):
        raise RuntimeError(f"{name} not found on PATH")


def _format_subprocess_failure(proc: subprocess.CompletedProcess, label: str) -> str:
    """Build a self-contained error message — exit code + stderr/stdout tail, or an explicit no-output hint."""
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    parts = [f"{label} failed (exit {proc.returncode})"]
    if stderr:
        parts.append(f"stderr: {stderr[-500:]}")
    if stdout and not stderr:
        parts.append(f"stdout: {stdout[-500:]}")
    if not stderr and not stdout:
        parts.append("no output captured — check binary install / PATH / network")
    return " | ".join(parts)


def _ensure_source_mp4(url: str, dest: Path, yt_dlp_bin: str = "yt-dlp") -> Path:
    """Download the source video to ``dest`` if it doesn't already exist. Returns ``dest``."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    _require_bin(yt_dlp_bin)
    # yt-dlp needs ffmpeg for muxing bestvideo+bestaudio into mp4. Pre-check so a
    # missing-ffmpeg failure surfaces as "ffmpeg not found on PATH" rather than an
    # opaque "yt-dlp source download failed:" with empty stderr.
    _require_bin("ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            yt_dlp_bin,
            "-f", SOURCE_FORMAT_SPEC,
            "--merge-output-format", "mp4",
            "-o", str(dest),
            "--no-progress",
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(_format_subprocess_failure(proc, "yt-dlp source download"))
    return dest


def _ffmpeg_extract_from_file(source: Path, seconds: float, out_path: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    """Extract a single frame from a local file at ``seconds`` into ``out_path``."""
    _require_bin(ffmpeg_bin)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-ss", f"{seconds:.3f}",
            "-i", str(source),
            "-vframes", "1",
            "-q:v", "2",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(_format_subprocess_failure(proc, f"ffmpeg extract at {seconds}s"))


def _ffmpeg_extract_streaming(
    url: str,
    seconds: float,
    out_path: Path,
    yt_dlp_bin: str = "yt-dlp",
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Stream the source via ``yt-dlp -g`` and grab one frame with ``ffmpeg -ss``."""
    _require_bin(yt_dlp_bin)
    _require_bin(ffmpeg_bin)
    g = subprocess.run(
        [yt_dlp_bin, "-f", SOURCE_FORMAT_SPEC, "-g", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if g.returncode != 0:
        raise RuntimeError(_format_subprocess_failure(g, "yt-dlp -g"))
    direct_url = g.stdout.strip().splitlines()[0]
    if not direct_url:
        raise RuntimeError("yt-dlp -g returned no URL")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-ss", f"{seconds:.3f}",
            "-i", direct_url,
            "-vframes", "1",
            "-q:v", "2",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(_format_subprocess_failure(proc, f"ffmpeg streaming extract at {seconds}s"))


def _detect_scene_timestamps(source: Path, threshold: float) -> list[float]:
    """Use PySceneDetect to find scene-boundary timestamps (seconds)."""
    from scenedetect import ContentDetector, detect

    scenes = detect(str(source), ContentDetector(threshold=threshold))
    return [s[0].get_seconds() for s in scenes]


def run(
    url: str,
    out_dir: Path | None = None,
    timestamps: list[float] | None = None,
    mode: str = "timestamps",
    interval: float | None = None,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    no_cache_source: bool = False,
    max_frames: int | None = DEFAULT_MAX_FRAMES,
    dedup: bool = True,
) -> list[Path]:
    """Extract frames per the chosen mode. Returns the list of written file paths.

    ``max_frames`` caps auto-selected frames (scene/interval) by even thinning;
    ``None`` or ``0`` disables the cap. Auto-selected near-duplicates are dropped
    before the cap unless ``dedup=False``. ``--timestamps`` is never capped or deduped.
    """
    extract_video_id(url)  # validate
    if out_dir is None:
        out_dir = cache_dir_for(url) / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_root = out_dir.parent

    if mode == "timestamps":
        if not timestamps:
            raise ValueError("mode=timestamps requires --timestamps")
        seconds_list = list(timestamps)
    elif mode == "interval":
        if not interval:
            raise ValueError("mode=interval requires --interval")
        meta = fetch_video_metadata(url)
        seconds_list = interval_timestamps(meta["duration"], interval)
    elif mode == "scene":
        if no_cache_source:
            raise ValueError("scene mode requires a downloaded source (drop --no-cache-source)")
        # Refuse before the source download: the caller must learn the fix
        # before paying for it (D4).
        require_extra("yt-frames --mode scene", "frames")
        source = _ensure_source_mp4(url, cache_root / "source.mp4")
        seconds_list = _scene_timestamps_with_fallback(source, scene_threshold, max_frames)
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    # Budget and dedup apply to auto-selection only. An explicit --timestamps list is
    # the caller's own decision, so it is neither thinned nor deduped (contract D1).
    if not seconds_list:
        return []

    if mode == "timestamps":
        source = None if no_cache_source else _ensure_source_mp4(url, cache_root / "source.mp4")
        written = _extract_candidates(url, seconds_list, out_dir, source, tolerate_failures=False)
        for path in written:
            print(f"Wrote: {path}")
        return written

    if no_cache_source:
        written = _extract_candidates(url, seconds_list, out_dir, None, tolerate_failures=True)
    else:
        source = _ensure_source_mp4(url, cache_root / "source.mp4")
        written = _extract_candidates(url, seconds_list, out_dir, source, tolerate_failures=True)

    return _apply_budget(written, max_frames, dedup)


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="yt-frames",
        description="Extract frames from a YouTube video at given timestamps / intervals / scene boundaries.",
    )
    parser.add_argument("url", help="YouTube URL or bare video id")
    parser.add_argument("--out", type=Path, default=None, help="Output dir (default: ./yt-cache/<vid>/frames/)")
    parser.add_argument(
        "--timestamps",
        default=None,
        help="Comma-separated mm:ss list; sets mode=timestamps. Example: 1:23,4:56,12:30",
    )
    parser.add_argument(
        "--mode",
        choices=["timestamps", "interval", "scene"],
        default=None,
        help="Extraction mode (auto: 'timestamps' if --timestamps given, else required).",
    )
    parser.add_argument("--interval", default=None, help="For --mode interval. Example: 30s / 1m / 2h")
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=DEFAULT_SCENE_THRESHOLD,
        help=f"PySceneDetect ContentDetector threshold (default {DEFAULT_SCENE_THRESHOLD}).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=(
            f"Cap on auto-selected frames (scene/interval), default {DEFAULT_MAX_FRAMES}: "
            "evenly thinned, first and last kept, tail never dropped. "
            "0 disables the cap (a uniform fallback scan stays at "
            f"{MIN_SCENE_FRAMES} frames - 'every frame of the video' is no scan at all). "
            "--timestamps is never capped."
        ),
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help=(
            "Keep near-duplicate frames. Dedup is ON by default for scene/interval "
            "selection (16x16 grayscale thumbnail, mean-abs-diff vs the last kept "
            "frame, threshold 2.0/255). --timestamps is never deduped."
        ),
    )
    parser.add_argument(
        "--no-cache-source",
        action="store_true",
        help="Stream source via yt-dlp -g | ffmpeg instead of caching source.mp4 on disk.",
    )
    args = parser.parse_args(argv)

    if args.max_frames < 0:
        parser.error("--max-frames must be >= 0 (0 disables the cap)")

    mode = args.mode or ("timestamps" if args.timestamps else None)
    if mode is None:
        parser.error("must give --timestamps or --mode {interval|scene}")

    timestamps: list[float] | None = None
    if args.timestamps:
        try:
            timestamps = [parse_timestamp_to_seconds(t) for t in args.timestamps.split(",") if t.strip()]
        except ValueError as e:
            parser.error(str(e))

    interval_seconds: float | None = None
    if args.interval:
        try:
            interval_seconds = parse_interval(args.interval)
        except ValueError as e:
            parser.error(str(e))

    try:
        run(
            args.url,
            out_dir=args.out,
            timestamps=timestamps,
            mode=mode,
            interval=interval_seconds,
            scene_threshold=args.scene_threshold,
            no_cache_source=args.no_cache_source,
            max_frames=args.max_frames,
            dedup=not args.no_dedup,
        )
    except (ValueError, RuntimeError, MetadataError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
