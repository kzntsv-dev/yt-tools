"""Real RapidOCR engine guard — the seam every other yt-ocr test mocks (task:2831).

``tests/test_ocr.py`` patches ``_load_engine`` in every integration case, so the
whole suite stayed green while the *published* ``[ocr]`` extra resolved to
rapidocr 3.9.x, whose ``params`` schema ``yt_tools/ocr.py`` does not speak. On a
clean ``pipx install "yt-tools-cli[ocr]"`` (0.24.0/0.24.1) the first
``yt-ocr`` run died with::

    error: Invalid OCR configuration.
    Example valid usage:
      from rapidocr import LangRec, OCRVersion, RapidOCR
      engine = RapidOCR(params={'Rec.ocr_version': OCRVersion.PPOCRV5, ...})

The mocked seam is exactly the seam that broke, so this module drives it for
real: build the engine through ``_load_engine``, OCR a frame the test renders
itself, and require text back. Detection is what makes it the guard it is —
``_load_engine`` is where rapidocr 3.9 rejects the config, so the failure lands
here as an ``OcrError``/``rapidocr`` error rather than in a consumer session.

Skipped — never failed — when rapidocr is absent: the whole module asks for the
``ocr_stack`` fixture (`tests/conftest.py`), so a leg without ``[ocr]`` gets one
named skip per test instead of a ``ModuleNotFoundError``, and the CI gate counts
those skips on the core leg and requires the tests to *run* on the ``full,ocr``
leg. The first construction downloads the ~10 MB PP-OCRv5 ONNX model, so the
network is a prerequisite of that leg, not of a plain ``pytest`` run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yt_tools import ocr as ocr_mod

# Every test here drives the real engine, so the gate is the whole module — see
# the docstring above and `tests/conftest.py`. Requesting the fixture (rather
# than a module-level `importorskip`) is also what applies the
# `requires_ocr` marker the gate counts.
pytestmark = pytest.mark.usefixtures("ocr_stack")

_VID = "abcDEF12345"
_URL = f"https://youtu.be/{_VID}"

# Rendered big and blocky on purpose: the guard must fail on a *configuration*
# mismatch, never because a serif glyph was misread at 12 px.
_RENDERED_TEXT = "OCR TEST"
_COLS = 720
_ROWS = 220


def _render_text_frame(path: Path, text: str = _RENDERED_TEXT) -> Path:
    """Draw ``text`` on a white canvas — a frame real OCR can read.

    ``cv2`` is not an optional nicety here: rapidocr depends on it (PP-OCRv5
    preprocessing), so using it adds no dependency the extra does not already
    carry. The Hershey font is built into OpenCV, so no font file is needed and
    the fixture stays a file the test writes, not a binary in git.
    """
    import cv2  # noqa: PLC0415 — heavy import, only reachable behind the [ocr] skip
    import numpy as np  # noqa: PLC0415

    canvas = np.full((_ROWS, _COLS, 3), 255, dtype=np.uint8)
    cv2.putText(
        canvas,
        text,
        (40, 150),
        cv2.FONT_HERSHEY_SIMPLEX,
        3.0,
        (0, 0, 0),
        8,
        cv2.LINE_AA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), canvas), f"could not write the rendered frame to {path}"
    return path


@pytest.fixture(scope="module")
def text_frame(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _render_text_frame(tmp_path_factory.mktemp("frames") / "frame_0023.jpg")


@pytest.fixture(scope="module")
def engine():
    """One engine per module: construction downloads the model, so it is not free."""
    return ocr_mod._load_engine("en")


@pytest.mark.parametrize("language", ocr_mod.SUPPORTED_LANGUAGES)
def test_every_supported_language_constructs_an_engine(language):
    """Every `--language` value must resolve to a model that exists (task:2833).

    Language → model is not a free mapping: the recognizer is per language, and
    PP-OCRv5 ships twelve of them with **no Japanese** among them — `--language
    ja` asked for a model that does not exist and died at construction, on 3.8.4
    as well as 3.9.2, while every test here (and the whole suite) stayed green
    because only `en` was ever built. This is the guard for that class: it
    exercises the real registry for every value the CLI advertises.

    Construction, not OCR: what breaks is model resolution, and a Japanese
    recognizer reading Latin text proves nothing. It does download a recognizer
    per language (~8-16 MB, once per runner) — the same lazy fetch the first real
    run pays, which is why this module already needs the network.
    """
    assert ocr_mod._load_engine(language) is not None


def test_load_engine_constructs_the_declared_params(engine):
    """The 3.9.x failure mode: RapidOCR refuses the 3.8.x-shaped ``params`` dict."""
    assert engine is not None
    # Not a mock assertion — `_load_engine` returns whatever RapidOCR built. If
    # the params schema drifts again, construction raises before we get here.
    assert hasattr(engine, "__call__"), "the engine handle must stay callable (engine(path))"


def test_engine_reads_text_from_a_rendered_frame(engine, text_frame):
    texts = ocr_mod._ocr_one(engine, text_frame)
    assert texts, (
        "a frame with rendered text produced no detections — the engine is not "
        "actually OCR-ing (model missing, or params silently ignored)"
    )
    joined = " ".join(texts).upper()
    assert "OCR" in joined, f"expected {_RENDERED_TEXT!r} to be read back, got {texts!r}"


def test_cli_real_engine_writes_nonempty_markdown(tmp_path, monkeypatch, text_frame):
    """The acceptance path end to end, minus YouTube: cached frame → ocr.md with text."""
    monkeypatch.chdir(tmp_path)
    cache_root = tmp_path / "yt-cache" / _VID
    frames_dir = cache_root / "frames"
    frames_dir.mkdir(parents=True)
    (frames_dir / text_frame.name).write_bytes(text_frame.read_bytes())

    out = tmp_path / "ocr.md"
    rc = ocr_mod.main([_URL, "--out", str(out)])

    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert body.strip(), "ocr.md must not be empty"
    assert "## [0:23]" in body
    assert "OCR" in body.upper()
