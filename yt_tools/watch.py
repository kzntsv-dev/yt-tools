"""yt-watch CLI — combined transcript + scene-frames in one markdown with sidecar embed.

Secondary use-case: a single ``watch.md`` per video that interleaves transcript paragraphs
with ``![](frames/frame_<mmss>.jpg)`` next to the matching scene boundary, so an agent can
``Read`` one document and "see" the video at the moments where it visually changes.
Two frames inside one second get distinct names (``frame_0130.jpg`` / ``frame_0130_250.jpg``,
issue:74) and the markdown links exactly the name written to disk.

Frame budget: the frames are auto-selected, so they go through the same pipeline as
``yt-frames`` — a uniform-scan fallback when the clip is static (contract D6), dedup of
near-duplicates (D7/D8) and even thinning to ``--max-frames`` (D2/D4), in that order
(D11). ``watch.md`` therefore carries neither hundreds of frames for a cut-heavy clip nor
a single frame for a talking head. Candidates the budget drops are removed from disk
(D15) so every ``![](frames/…)`` link points at a file the agent can actually read;
counts and the fallback warning go to stderr, because stdout stays the one-line path of
the artefact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yt_tools._metadata import MetadataError, fetch_video_metadata
from yt_tools.core import (
    cache_dir_for,
    extract_video_id,
    force_utf8_streams,
    format_seconds_to_mmss,
    frame_filename,
)
from yt_tools.extras import require_extra
from yt_tools.frames import (
    DEFAULT_MAX_FRAMES,
    DEFAULT_SCENE_THRESHOLD,
    MIN_SCENE_FRAMES,
    _ensure_source_mp4,
    _ffmpeg_extract_from_file,
    _remove_quietly,
    _scene_timestamps_with_fallback,
    _select_frames,
)
from yt_tools.markdown import (
    DEFAULT_PARAGRAPH_GAP_SECONDS,
    Snippet,
    _group_paragraphs,
)
from yt_tools.transcript import TranscriptUnavailable, _fetch_snippets


# Пояснение в шапке кадрового артефакта: агент читает watch.md, а не stderr, и без
# этой строки «текста нет» выглядит как «текст не понадобился».
NO_TRANSCRIPT_NOTE = "Transcript unavailable — frames only"


def _render_interleaved(
    title: str,
    channel: str,
    duration: int,
    lang: str,
    url: str,
    snippets: list[Snippet],
    frame_timestamps: list[float],
    frames_subdir: str = "frames",
    paragraph_gap_seconds: float = DEFAULT_PARAGRAPH_GAP_SECONDS,
    transcript_note: str | None = None,
) -> str:
    paragraphs = _group_paragraphs(snippets, paragraph_gap_seconds)
    duration_str = format_seconds_to_mmss(float(duration)) if duration else "?"

    lines: list[str] = [f"# {title}", ""]
    meta_bits: list[str] = []
    if channel:
        meta_bits.append(f"**Channel:** {channel}")
    meta_bits.append(f"**Duration:** {duration_str}")
    if lang:
        meta_bits.append(f"**Lang:** {lang}")
    if url:
        meta_bits.append(f"**URL:** {url}")
    lines.append("  ".join(meta_bits))
    lines.append("")
    if transcript_note:
        lines.append(f"_{transcript_note}_")
        lines.append("")
    lines.append("---")
    lines.append("")

    def _emit_image(ts: float) -> None:
        label = format_seconds_to_mmss(ts)
        lines.append(f"![scene at {label}]({frames_subdir}/{frame_filename(ts)})")
        lines.append("")

    # Same helper as the extractor, so the link is the file that is on disk: a second
    # holding two frames yields two names (issue:74), never one overwritten file.
    frame_iter = iter(sorted(frame_timestamps))
    next_frame: float | None = next(frame_iter, None)
    para_starts = [p[0] for p in paragraphs]

    for i, (start, text) in enumerate(paragraphs):
        next_para_start = para_starts[i + 1] if i + 1 < len(paragraphs) else float("inf")
        # Frames before this paragraph (only possible for i == 0).
        while next_frame is not None and next_frame < start:
            _emit_image(next_frame)
            next_frame = next(frame_iter, None)
        lines.append(f"[{format_seconds_to_mmss(start)}] {text}")
        lines.append("")
        # Frames that fall inside this paragraph (between its start and the next paragraph).
        while next_frame is not None and next_frame < next_para_start:
            _emit_image(next_frame)
            next_frame = next(frame_iter, None)

    # Any trailing frames after the last paragraph (no transcript exists for them).
    while next_frame is not None:
        _emit_image(next_frame)
        next_frame = next(frame_iter, None)

    return "\n".join(lines).rstrip() + "\n"


def _fetch_snippets_or_degrade(video_id: str, languages: list[str]) -> tuple[list[Snippet], str, str | None]:
    """Transcript, or an empty transcript plus the reason for the markdown header.

    The artefact of this CLI is frames with — when they exist — the text next to them, so
    a video without captions loses the text, not the frames (requirements AC9). Only the
    declared refusal degrades; an unexpected bug still fails the run loudly.
    """
    try:
        snippets, lang = _fetch_snippets(video_id, languages)
    except TranscriptUnavailable as e:
        print(f"warning: no transcript for this video: {e} — writing frames-only watch.md", file=sys.stderr)
        return [], "", f"{NO_TRANSCRIPT_NOTE}: {e}"
    return snippets, lang, None


def run(
    url: str,
    out_dir: Path | None = None,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    languages: list[str] | None = None,
    max_frames: int | None = DEFAULT_MAX_FRAMES,
    dedup: bool = True,
) -> Path:
    """Render ``watch.md`` for ``url`` and return its path.

    The embedded frames are auto-selected, so they obey the same budget as
    ``yt-frames``: ``max_frames`` caps them by even thinning (``None`` / ``0`` disables
    the cap) and near-duplicates are dropped before the cap unless ``dedup=False``.
    """
    languages = languages or ["en"]
    video_id = extract_video_id(url)
    # `yt-watch` is transcript + scene-frames, and scene detection needs the
    # [frames] extra: refuse before the metadata/transcript fetches (D4).
    require_extra("yt-watch", "frames")
    if out_dir is None:
        out_dir = cache_dir_for(url)
    frames_dir = out_dir / "frames"

    try:
        meta = fetch_video_metadata(url)
    except MetadataError as e:
        print(f"warning: {e} — using minimal metadata", file=sys.stderr)
        meta = {"title": video_id, "channel": "", "duration": 0, "url": url}

    snippets, lang, transcript_note = _fetch_snippets_or_degrade(video_id, languages)

    source = _ensure_source_mp4(url, out_dir / "source.mp4")
    # Auto-selection shares yt-frames' entry point: static footage falls back to an even
    # scan of the timeline instead of handing the agent one frame for the whole video
    # (D6). A sub-millisecond pair is the same moment and would yield the same file
    # name, so it is collapsed before extraction rather than linked twice.
    timestamps = sorted(_scene_timestamps_with_fallback(source, scene_threshold, max_frames))

    candidates: list[tuple[float, Path]] = []
    seen_names: set[str] = set()
    failures: list[tuple[float, str]] = []
    for s in timestamps:
        name = frame_filename(s)
        if name in seen_names:
            continue
        seen_names.add(name)
        out_path = frames_dir / name
        try:
            if not out_path.exists():
                _ffmpeg_extract_from_file(source, s, out_path)
        except RuntimeError as e:
            # Тот же fail-open, что у автоподбора yt-frames: контейнер может обещать
            # больше секунд, чем декодирует, а терять из-за одного такого кандидата весь
            # watch.md незачем.
            failures.append((s, str(e)))
            continue
        candidates.append((s, out_path.resolve()))

    if failures:
        print(
            f"note: skipped {len(failures)} candidate frame(s) ffmpeg could not extract"
            f" (first at {failures[0][0]:.3f}s — past the end of the video?)",
            file=sys.stderr,
        )
    if not candidates and failures:
        # Обратная сторона fail-open: пустой результат — это не «видео без кадров», а
        # сломанный инструмент. Пустой список моментов (детекция ничего не дала)
        # ошибкой не считается — тогда честно остаётся только транскрипт.
        raise RuntimeError(f"no frames extracted: {failures[0][1]}")

    # Dedup then cap (D11); the dropped candidates go away, so every markdown link is a
    # file on disk (D15). Unlike yt-frames, nothing is printed per frame: the artefact of
    # this CLI is watch.md, and its stdout contract is a single line.
    survivors = set(_select_frames([path for _, path in candidates], max_frames, dedup))
    for _, path in candidates:
        if path not in survivors:
            _remove_quietly(path)
    frame_timestamps = [s for s, path in candidates if path in survivors]

    md = _render_interleaved(
        title=meta["title"],
        channel=meta["channel"],
        duration=meta["duration"],
        lang=lang,
        url=meta["url"],
        snippets=snippets,
        frame_timestamps=frame_timestamps,
        transcript_note=transcript_note,
    )
    watch_md = out_dir / "watch.md"
    watch_md.write_text(md, encoding="utf-8")
    return watch_md.resolve()


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="yt-watch",
        description="Combined transcript + scene-frames in one markdown (sidecar embed).",
    )
    parser.add_argument("url", help="YouTube URL or bare video id")
    parser.add_argument("--out", type=Path, default=None, help="Output dir (default: ./yt-cache/<vid>/)")
    parser.add_argument(
        "--scene-threshold",
        type=float,
        default=DEFAULT_SCENE_THRESHOLD,
        help=f"PySceneDetect ContentDetector threshold (default {DEFAULT_SCENE_THRESHOLD}).",
    )
    parser.add_argument("--lang", default="en", help="Comma-separated language preference (default: en).")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=(
            f"Cap on auto-selected embedded frames, default {DEFAULT_MAX_FRAMES}: evenly "
            "thinned, first and last kept, tail never dropped. 0 disables the cap (a "
            f"uniform fallback scan stays at {MIN_SCENE_FRAMES} frames - 'every frame of "
            "the video' is no scan at all)."
        ),
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help=(
            "Keep near-duplicate embedded frames. Dedup is ON by default "
            "(16x16 grayscale thumbnail, mean-abs-diff vs the last kept frame, "
            "threshold 2.0/255)."
        ),
    )
    args = parser.parse_args(argv)

    if args.max_frames < 0:
        parser.error("--max-frames must be >= 0 (0 disables the cap)")

    languages = [lang.strip() for lang in args.lang.split(",") if lang.strip()]
    try:
        path = run(
            args.url,
            out_dir=args.out,
            scene_threshold=args.scene_threshold,
            languages=languages,
            max_frames=args.max_frames,
            dedup=not args.no_dedup,
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
