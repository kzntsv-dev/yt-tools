"""yt-ocr CLI — OCR on cached frames → ``./yt-cache/<vid>/ocr.md``.

Engine is RapidOCR 3.x (PP-OCRv5 models via onnxruntime). Lazy-imported inside
``_load_engine`` so ``import yt_tools`` stays cheap and a missing ``[ocr]``
extra surfaces only when ``yt-ocr`` actually runs.

Two modes mirror existing CLIs:

  ``yt-ocr URL``                         # batch-on-cache: OCR every frame
                                         #   under ./yt-cache/<vid>/frames/
  ``yt-ocr URL --timestamps 1:30,2:45``  # standalone: extract via internal
                                         #   yt-frames helper first, then OCR
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from yt_tools.core import (
    cache_dir_for,
    extract_video_id,
    force_utf8_streams,
    format_seconds_to_mmss,
    parse_timestamp_to_seconds,
)
from yt_tools.extras import format_missing_extra

ENGINE_LABEL = "RapidOCR (PP-OCRv5)"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "ru", "ja", "zh", "multi")

_FRAME_RE = re.compile(r"^frame_(\d+)(?:_(\d+))?\.jpg$")
_NO_TEXT_MARKER = "_(no text detected)_"


class OcrError(RuntimeError):
    """Any OCR-side failure: missing extra, missing cache, model issues, etc."""


# --- frame discovery ---------------------------------------------------------


def _parse_frame_seconds(name: str) -> int | None:
    """Parse ``frame_<mmss>.jpg`` / ``frame_<hhmmss>.jpg`` → integer seconds.

    A trailing sub-second part (``frame_0130_250.jpg`` = 90.25s, issue:74) is part of
    the uniqueness scheme, not of the timestamp: the second it belongs to is unchanged.
    Returns None for any basename that doesn't match the contract — caller
    silently skips those files (foreign artefacts shouldn't break a batch).
    """
    m = _FRAME_RE.match(name)
    if not m:
        return None
    digits = m.group(1)
    if len(digits) == 4:
        return int(digits[:2]) * 60 + int(digits[2:])
    if len(digits) == 6:
        return int(digits[:2]) * 3600 + int(digits[2:4]) * 60 + int(digits[4:])
    return None


def discover_frames(frames_dir: Path) -> list[tuple[int, Path]]:
    """Return ``[(seconds, path), ...]`` sorted ascending for ``frame_*.jpg``."""
    if not frames_dir.is_dir():
        return []
    out: list[tuple[int, Path]] = []
    for p in frames_dir.glob("frame_*.jpg"):
        secs = _parse_frame_seconds(p.name)
        if secs is None:
            continue
        out.append((secs, p))
    # Timestamp first, then name: several frames of one second differ only by their
    # sub-second part, and glob order is filesystem-dependent (issue:74).
    out.sort(key=lambda item: (item[0], item[1].name))
    return out


# --- engine factory (lazy import) -------------------------------------------


def _build_params(language: str, langrec_enum, ocrversion_enum=None) -> dict:
    """Map user-facing language → RapidOCR params dict.

    Forces ``PP-OCRv5`` across Det / Cls / Rec when ``ocrversion_enum`` is
    supplied (RapidOCR 3.8.x defaults the bundled-model dance to v4 — we want
    v5 per design spec). For ``multi``, returns params **without** a
    ``Rec.lang_type`` override so RapidOCR picks its default multilingual
    model (Chinese+English).
    """
    params: dict = {}
    if ocrversion_enum is not None:
        v5 = ocrversion_enum.PPOCRV5
        params["Det.ocr_version"] = v5
        params["Cls.ocr_version"] = v5
        params["Rec.ocr_version"] = v5
    if language == "multi":
        return params
    mapping = {
        "en": getattr(langrec_enum, "EN", None),
        "ru": getattr(langrec_enum, "CYRILLIC", None),
        "ja": getattr(langrec_enum, "JAPAN", None),
        "zh": getattr(langrec_enum, "CH", None),
    }
    lang_type = mapping.get(language)
    if lang_type is None:
        raise OcrError(
            f"unknown language: {language!r} "
            f"(supported: {', '.join(SUPPORTED_LANGUAGES)})"
        )
    params["Rec.lang_type"] = lang_type
    return params


def _load_engine(language: str):
    """Import rapidocr lazily and instantiate the engine.

    Lazy: ``import yt_tools`` must not pull rapidocr / onnxruntime — the
    ``[ocr]`` extra is opt-in and the rest of the suite must keep working
    without it.
    """
    try:
        from rapidocr import LangRec, OCRVersion, RapidOCR  # noqa: PLC0415
        import onnxruntime  # noqa: F401, PLC0415 — the [ocr] extra ships both; a half-installed pair must refuse here, not crash later
    except ImportError as e:
        # Same wording as every other CLI (D4) — the message lives in one place.
        raise OcrError(format_missing_extra("yt-ocr", "ocr")) from e
    params = _build_params(language, LangRec, OCRVersion)
    return RapidOCR(params=params)


def _ocr_one(engine, image_path: Path) -> list[str]:
    """Run engine on one image, return list of detected text strings.

    Handles RapidOCR 3.x result-object shape (``.txts``) and the 2.x tuple
    fallback ``(boxes, txts, scores)``. Empty / None → ``[]`` (caller renders
    the no-text-detected marker).
    """
    result = engine(str(image_path))
    if result is None:
        return []
    txts = getattr(result, "txts", None)
    if txts is not None:
        return [t for t in txts if t]
    if isinstance(result, tuple) and len(result) >= 2 and result[1]:
        return [t for t in result[1] if t]
    return []


# --- pure render -------------------------------------------------------------


def ocr_to_markdown(
    video_id: str,
    frames: list[tuple[int, list[str]]],
    language: str,
    generated: str | None = None,
) -> str:
    """Render ``[(seconds, [text-lines]), ...]`` as markdown with [mm:ss] anchors."""
    if generated is None:
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"# OCR — {video_id}",
        "",
        (
            f"generated: {generated} · engine: {ENGINE_LABEL}"
            f" · language: {language} · frames: {len(frames)}"
        ),
        "",
        "---",
        "",
    ]
    for secs, texts in frames:
        anchor = format_seconds_to_mmss(float(secs))
        lines.append(f"## [{anchor}]")
        lines.append("")
        if texts:
            lines.extend(texts)
        else:
            lines.append(_NO_TEXT_MARKER)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- main orchestration ------------------------------------------------------


def run(
    url: str,
    out: Path | None = None,
    timestamps: list[float] | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> Path:
    """OCR every cached frame for ``url``. Returns absolute path of ``ocr.md``."""
    video_id = extract_video_id(url)
    cache_root = cache_dir_for(url)
    frames_dir = cache_root / "frames"

    if timestamps:
        from yt_tools import frames as frames_mod  # noqa: PLC0415

        frames_mod.run(
            url,
            out_dir=frames_dir,
            timestamps=timestamps,
            mode="timestamps",
        )

    discovered = discover_frames(frames_dir)
    if not discovered:
        raise OcrError(
            f"no cached frames in {frames_dir} — "
            f"run `yt-frames {url}` first, or pass --timestamps to extract on the fly"
        )

    engine = _load_engine(language)

    per_frame: list[tuple[int, list[str]]] = []
    for secs, path in discovered:
        texts = _ocr_one(engine, path)
        per_frame.append((secs, texts))

    md = ocr_to_markdown(video_id, per_frame, language=language)

    if out is None:
        out = cache_root / "ocr.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out.resolve()


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="yt-ocr",
        description=(
            "Run OCR on cached frames of a YouTube video; render markdown with "
            "[mm:ss] anchors. Engine: RapidOCR (PP-OCRv5) via onnxruntime."
        ),
    )
    parser.add_argument("url", help="YouTube URL or bare video id")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: ./yt-cache/<vid>/ocr.md)",
    )
    parser.add_argument(
        "--timestamps",
        default=None,
        help="Comma-separated mm:ss list. If given, extract frames via yt-frames first, then OCR.",
    )
    parser.add_argument(
        "--language",
        choices=list(SUPPORTED_LANGUAGES),
        default=DEFAULT_LANGUAGE,
        help=(
            f"OCR language (default: {DEFAULT_LANGUAGE}). "
            "multi = PP-OCR multilingual (Chinese+English)."
        ),
    )
    args = parser.parse_args(argv)

    timestamps: list[float] | None = None
    if args.timestamps:
        try:
            timestamps = [
                parse_timestamp_to_seconds(t)
                for t in args.timestamps.split(",")
                if t.strip()
            ]
        except ValueError as e:
            parser.error(str(e))

    try:
        path = run(
            args.url,
            out=args.out,
            timestamps=timestamps,
            language=args.language,
        )
    except OcrError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
