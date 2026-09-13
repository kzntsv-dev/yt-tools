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
#: Languages whose recognizer is not PP-OCRv5 — the label has to name what ran.
_ENGINE_LABEL_JA = "RapidOCR (PP-OCRv5 det/cls + PP-OCRv4 ja rec)"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "ru", "ja", "zh", "multi")

_FRAME_RE = re.compile(r"^frame_(\d+)(?:_(\d+))?\.jpg$")
_NO_TEXT_MARKER = "_(no text detected)_"

#: Language → (``LangRec`` member name, recognizer ``OCRVersion`` member name).
#:
#: Detection and classification are language-agnostic in the v5 family (one `ch`
#: model each), but the recognizer is per language — and PP-OCRv5 ships twelve of
#: them (arabic, ch, cyrillic, devanagari, el, en, eslav, korean, latin, ta, te,
#: th), **Japanese not among them**: `japan_PP-OCRv4_rec_mobile` is the only
#: Japanese recognizer in both 3.8.x and 3.9.x registries. So the version is
#: named per language instead of forced globally. Two failure modes meet here, and
#: both shipped: with `Rec.ocr_version=PP-OCRv5` + `japan` the library's lenient
#: fallback used to pick the v4 model (working by luck), and once `model_type` is
#: named — which it must be, or the config depends on the installed release's
#: defaults ([[issue:77]]) — the fallback is skipped and construction raises
#: `Invalid OCR configuration`. Verified by building the engine for every
#: ``SUPPORTED_LANGUAGES`` entry on rapidocr 3.8.4 and 3.9.2 ([[task:2833]]).
_LANGUAGE_REC_VERSION: dict[str, tuple[str, str]] = {
    "en": ("EN", "PPOCRV5"),
    "ru": ("CYRILLIC", "PPOCRV5"),
    "ja": ("JAPAN", "PPOCRV4"),
    "zh": ("CH", "PPOCRV5"),
}


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


def _build_params(
    language: str,
    langrec_enum,
    ocrversion_enum=None,
    modeltype_enum=None,
) -> dict:
    """Map user-facing language → RapidOCR params dict.

    Forces ``PP-OCRv5`` across Det / Cls / Rec when ``ocrversion_enum`` is
    supplied (RapidOCR 3.8.x defaults the bundled-model dance to v4 — we want
    v5 per design spec) **and names the model type** (``mobile``) alongside it,
    when ``modeltype_enum`` is supplied.

    Both halves are needed, and that is the whole lesson of [[issue:77]]: the
    model type is a *version-dependent default*, not a constant. 3.8.x defaults
    every component to ``PP-OCRv4 + mobile`` — which happens to be valid for v5
    too; 3.9.x moved Det/Rec to ``PP-OCRv6 + small``, and the v5 family ships
    ``mobile``/``server`` only, so a version-only pin asked for "PP-OCRv5 … small"
    and RapidOCR refused the config at construction. Pinning the pair makes the
    params mean the same thing on every supported release.

    For ``multi``, returns params **without** a ``Rec.lang_type`` override so
    RapidOCR picks its default multilingual model (Chinese+English).
    """
    params: dict = {}
    if ocrversion_enum is not None:
        v5 = ocrversion_enum.PPOCRV5
        params["Det.ocr_version"] = v5
        params["Cls.ocr_version"] = v5
        # Overridden per language below: not every supported language has a v5
        # recognizer (see _LANGUAGE_REC_VERSION).
        params["Rec.ocr_version"] = v5
    if modeltype_enum is not None:
        # `mobile` (not `server`): the small/fast bundle, matching the CPU-only
        # onnxruntime install the extra brings, and the one key the v5 family
        # has for every supported language/component.
        mobile = modeltype_enum.MOBILE
        params["Det.model_type"] = mobile
        params["Cls.model_type"] = mobile
        params["Rec.model_type"] = mobile
    if language == "multi":
        return params
    member, rec_version = _LANGUAGE_REC_VERSION.get(language, (None, None))
    lang_type = getattr(langrec_enum, member, None) if member else None
    if lang_type is None:
        raise OcrError(
            f"unknown language: {language!r} "
            f"(supported: {', '.join(SUPPORTED_LANGUAGES)})"
        )
    params["Rec.lang_type"] = lang_type
    if ocrversion_enum is not None:
        params["Rec.ocr_version"] = getattr(ocrversion_enum, rec_version)
    return params


def _load_engine(language: str):
    """Import rapidocr lazily and instantiate the engine.

    Lazy: ``import yt_tools`` must not pull rapidocr / onnxruntime — the
    ``[ocr]`` extra is opt-in and the rest of the suite must keep working
    without it.
    """
    try:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR  # noqa: PLC0415
        import onnxruntime  # noqa: F401, PLC0415 — the [ocr] extra ships both; a half-installed pair must refuse here, not crash later
    except ImportError as e:
        # Same wording as every other CLI (D4) — the message lives in one place.
        raise OcrError(format_missing_extra("yt-ocr", "ocr")) from e
    params = _build_params(language, LangRec, OCRVersion, ModelType)
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


def engine_label(language: str) -> str:
    """Header label naming the recognizer that actually runs for ``language``.

    Japanese is the one supported language with no PP-OCRv5 recognizer, so a
    blanket "PP-OCRv5" line would be a claim the artefact cannot back — and the
    header is the only place a reader (or an agent comparing two runs) can see
    which models produced the text.
    """
    _, rec_version = _LANGUAGE_REC_VERSION.get(language, (None, "PPOCRV5"))
    return _ENGINE_LABEL_JA if rec_version == "PPOCRV4" else ENGINE_LABEL


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
            f"generated: {generated} · engine: {engine_label(language)}"
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
            "multi = PP-OCR multilingual (Chinese+English). "
            "ja uses the PP-OCRv4 recognizer: no PP-OCRv5 Japanese model exists."
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
