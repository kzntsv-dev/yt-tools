"""Tests for yt-ocr — OCR on cached frames + markdown render.

Layers: ``_parse_frame_seconds`` (filename → seconds), ``discover_frames``
(directory glob + sort), ``_build_params`` (pure language → RapidOCR params
mapping), ``ocr_to_markdown`` (pure render with empty-frame marker), CLI
integration with the ``_load_engine`` boundary mocked (engine factory is the
lazy-import seam — production tests don't need rapidocr installed).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yt_tools import ocr as ocr_mod
from yt_tools.ocr import (
    OcrError,
    _build_params,
    _parse_frame_seconds,
    discover_frames,
    ocr_to_markdown,
)

_VID = "abcDEF12345"
_URL = f"https://youtu.be/{_VID}"


# --- _parse_frame_seconds ----------------------------------------------------


def test_parse_frame_seconds_mmss():
    assert _parse_frame_seconds("frame_0023.jpg") == 23
    assert _parse_frame_seconds("frame_1245.jpg") == 12 * 60 + 45
    assert _parse_frame_seconds("frame_0000.jpg") == 0


def test_parse_frame_seconds_hhmmss():
    # frame_<hhmmss>.jpg for long videos
    assert _parse_frame_seconds("frame_012345.jpg") == 1 * 3600 + 23 * 60 + 45


def test_parse_frame_seconds_rejects_non_frame_basename():
    assert _parse_frame_seconds("foo.jpg") is None
    assert _parse_frame_seconds("frame_.jpg") is None
    assert _parse_frame_seconds("frame_abc.jpg") is None


# --- discover_frames ---------------------------------------------------------


def test_discover_frames_returns_sorted_pairs(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    # Create out-of-order to verify sort
    (frames / "frame_1230.jpg").write_bytes(b"x")
    (frames / "frame_0023.jpg").write_bytes(b"x")
    (frames / "frame_0145.jpg").write_bytes(b"x")
    (frames / "not_a_frame.jpg").write_bytes(b"x")  # ignored

    out = discover_frames(frames)
    assert [secs for secs, _ in out] == [23, 105, 750]
    # paths are real and exist
    for _, p in out:
        assert p.exists()


def test_discover_frames_missing_dir_returns_empty(tmp_path):
    assert discover_frames(tmp_path / "does-not-exist") == []


def test_discover_frames_empty_dir_returns_empty(tmp_path):
    (tmp_path / "frames").mkdir()
    assert discover_frames(tmp_path / "frames") == []


# --- _build_params -----------------------------------------------------------


class _FakeLangRec:
    EN = "lang::en"
    CYRILLIC = "lang::cyrillic"
    JAPAN = "lang::japan"
    CH = "lang::ch"


class _FakeOCRVersion:
    PPOCRV5 = "ocrver::v5"
    PPOCRV4 = "ocrver::v4"


class _FakeModelType:
    MOBILE = "model::mobile"


def test_build_params_en_maps_to_LangRec_EN():
    assert _build_params("en", _FakeLangRec)["Rec.lang_type"] == "lang::en"


def test_build_params_ru_maps_to_LangRec_CYRILLIC():
    assert _build_params("ru", _FakeLangRec)["Rec.lang_type"] == "lang::cyrillic"


def test_build_params_ja_maps_to_LangRec_JAPAN():
    assert _build_params("ja", _FakeLangRec)["Rec.lang_type"] == "lang::japan"


def test_build_params_zh_maps_to_LangRec_CH():
    assert _build_params("zh", _FakeLangRec)["Rec.lang_type"] == "lang::ch"


def test_build_params_multi_returns_no_lang_type():
    # multi → use RapidOCR default (Chinese+English multilingual model)
    p = _build_params("multi", _FakeLangRec)
    assert "Rec.lang_type" not in p


def test_build_params_unknown_raises():
    with pytest.raises(OcrError):
        _build_params("xx", _FakeLangRec)


def test_build_params_forces_pp_ocrv5_across_components():
    """RapidOCR 3.8.x defaults to v4 unless we explicitly opt into v5 —
    align all three components (det/cls/rec) per design spec."""
    p = _build_params("en", _FakeLangRec, _FakeOCRVersion)
    assert p["Det.ocr_version"] == "ocrver::v5"
    assert p["Cls.ocr_version"] == "ocrver::v5"
    assert p["Rec.ocr_version"] == "ocrver::v5"


def test_build_params_multi_still_forces_v5():
    """``multi`` skips lang_type override but must still pin v5."""
    p = _build_params("multi", _FakeLangRec, _FakeOCRVersion)
    assert p["Rec.ocr_version"] == "ocrver::v5"
    assert "Rec.lang_type" not in p


# task:2833 — the model_type half of the PP-OCRv5 pin. Pinning only the version
# left model_type at rapidocr's default, which is a *version-dependent* value:
# 3.8 defaults every component to PP-OCRv4 + "mobile" (valid for v5), 3.9 moved
# Det/Rec to PP-OCRv6 + "small" — and PP-OCRv5 ships mobile/server only, so
# "PP-OCRv5 … small" resolves to nothing and construction raises
# "error: Invalid OCR configuration" ([[issue:77]]). Naming the model type makes
# the config independent of whatever default the installed release ships.
def test_build_params_pins_the_model_type_alongside_the_version():
    p = _build_params("en", _FakeLangRec, _FakeOCRVersion, _FakeModelType)
    assert p["Det.model_type"] == "model::mobile"
    assert p["Cls.model_type"] == "model::mobile"
    assert p["Rec.model_type"] == "model::mobile"


def test_build_params_multi_also_pins_the_model_type():
    p = _build_params("multi", _FakeLangRec, _FakeOCRVersion, _FakeModelType)
    assert p["Rec.model_type"] == "model::mobile"
    assert "Rec.lang_type" not in p


def test_build_params_without_a_modeltype_enum_stays_silent_about_it():
    """The version/model pins travel together: no enum, no keys.

    ``_build_params`` is called with the rapidocr enums only from
    ``_load_engine``; the older signature (no ``ModelType``) must not
    silently emit a half pin — a config with ``model_type`` but no
    ``ocr_version`` (or the reverse) is exactly the drift this pins against.
    """
    p = _build_params("en", _FakeLangRec, _FakeOCRVersion)
    assert not [k for k in p if "model_type" in k] or p["Det.ocr_version"] == "ocrver::v5"


# task:2833 — PP-OCRv5 ships twelve recognizers and Japanese is not one of them
# (its registry entry is v4-only, identically in 3.8.x and 3.9.x). Asking for
# v5 + japan resolves to nothing; before this pin the v4 model was found by the
# library's lenient fallback, and once model_type is named — as it must be, so
# the config stops depending on the installed release's defaults — that fallback
# is skipped and construction raises. Pinning the recognizer per language makes
# `--language ja` work by design rather than by luck.
def test_build_params_ja_uses_the_v4_recognizer():
    p = _build_params("ja", _FakeLangRec, _FakeOCRVersion, _FakeModelType)
    assert p["Rec.lang_type"] == "lang::japan"
    assert p["Rec.ocr_version"] == "ocrver::v4"
    # ...while detection and classification stay on v5: they are
    # language-agnostic (one `ch` model each) in the v5 family.
    assert p["Det.ocr_version"] == "ocrver::v5"
    assert p["Cls.ocr_version"] == "ocrver::v5"


def test_build_params_languages_with_a_v5_recognizer_stay_on_v5():
    for lang in ("en", "ru", "zh"):
        p = _build_params(lang, _FakeLangRec, _FakeOCRVersion, _FakeModelType)
        assert p["Rec.ocr_version"] == "ocrver::v5", lang


def test_build_params_multi_keeps_the_v5_ch_recognizer():
    """`multi` means RapidOCR's default recognizer — which for v5 is `ch`."""
    p = _build_params("multi", _FakeLangRec, _FakeOCRVersion, _FakeModelType)
    assert p["Rec.ocr_version"] == "ocrver::v5"
    assert "Rec.lang_type" not in p


# --- ocr_to_markdown ---------------------------------------------------------


def test_render_header_contains_engine_language_count():
    md = ocr_to_markdown(
        _VID,
        [(23, ["hello"]), (105, ["world"])],
        language="en",
        generated="2026-05-28T14:23:11Z",
    )
    assert f"# OCR — {_VID}" in md
    assert "engine: RapidOCR" in md
    assert "language: en" in md
    assert "frames: 2" in md
    assert "2026-05-28T14:23:11Z" in md


def test_render_per_frame_blocks_use_mmss_anchors():
    md = ocr_to_markdown(
        _VID,
        [(23, ["GRANULITA VERSIO"]), (107, ["PARAMETERS"])],
        language="en",
        generated="2026-05-28T14:23:11Z",
    )
    # 23s → 0:23 ; 107s → 1:47
    assert "## [0:23]" in md
    assert "## [1:47]" in md
    assert "GRANULITA VERSIO" in md
    assert "PARAMETERS" in md


def test_render_empty_text_uses_no_text_marker():
    md = ocr_to_markdown(
        _VID,
        [(138, [])],  # empty list — engine found no text
        language="en",
        generated="2026-05-28T14:23:11Z",
    )
    assert "## [2:18]" in md
    assert "_(no text detected)_" in md


def test_render_zero_frames_still_emits_header():
    md = ocr_to_markdown(_VID, [], language="en", generated="2026-05-28T14:23:11Z")
    assert "frames: 0" in md


# task:2833 — the header line is the only trace of *what* read the frames, so it
# must not claim PP-OCRv5 for a language that has no v5 recognizer (Japanese is
# read by the v4 recognizer, see _LANGUAGE_REC_VERSION).
def test_render_names_the_v4_recognizer_for_japanese():
    md = ocr_to_markdown(_VID, [(23, ["こんにちは"])], language="ja")
    assert "PP-OCRv4" in md, "the ja header must name the recognizer that actually ran"
    assert "language: ja" in md


def test_render_keeps_the_plain_v5_label_for_languages_that_have_one():
    md = ocr_to_markdown(_VID, [(23, ["hello"])], language="en")
    assert "engine: RapidOCR (PP-OCRv5)" in md
    assert "PP-OCRv4" not in md


# --- _load_engine: friendly missing-extra hint -------------------------------


# [test-modify: test_load_engine_friendly_hint_when_rapidocr_missing: was
#  `assert "pipx inject yt-tools-cli rapidocr onnxruntime" in msg`; is the same
#  with the quoted, bounded specs; reason: task:2831 — the hint carries the
#  rapidocr pin (an inject bypasses the extra metadata) and quotes every spec so
#  the pasted command is safe in bash/cmd/PowerShell.]
def test_load_engine_friendly_hint_when_rapidocr_missing(monkeypatch):
    """Stick None into sys.modules['rapidocr'] so `from rapidocr import ...`
    raises ImportError; verify the friendly hint is built into the error."""
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    with pytest.raises(OcrError) as exc_info:
        ocr_mod._load_engine("en")
    msg = str(exc_info.value)
    assert 'pipx inject yt-tools-cli "rapidocr>=3.8,<4" "onnxruntime>=1.18"' in msg
    assert "pip install 'yt-tools-cli[ocr]'" in msg


# --- CLI integration ---------------------------------------------------------


def _fake_engine_returning(texts_per_call: list[list[str]]):
    """Return a callable mock engine that yields ``texts_per_call[i]`` on the i-th call."""
    engine = MagicMock()
    results = []
    for texts in texts_per_call:
        r = MagicMock()
        r.txts = tuple(texts)
        results.append(r)
    engine.side_effect = results
    return engine


def _seed_frames(cache_root: Path, mmss_list: list[str]) -> None:
    """Drop ``frame_<mmss>.jpg`` placeholders under ``<cache_root>/frames/``."""
    frames = cache_root / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for mmss in mmss_list:
        (frames / f"frame_{mmss}.jpg").write_bytes(b"fake-jpg")


def test_cli_batch_mode_writes_ocr_md_with_three_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache_root = tmp_path / "yt-cache" / _VID
    _seed_frames(cache_root, ["0023", "0107", "0218"])
    engine = _fake_engine_returning([
        ["GRANULITA VERSIO", "Granular Texture Synthesizer"],
        ["PARAMETERS"],
        [],  # no text detected
    ])

    with patch.object(ocr_mod, "_load_engine", return_value=engine):
        rc = ocr_mod.main([_URL])

    assert rc == 0
    md_path = cache_root / "ocr.md"
    assert md_path.exists()
    body = md_path.read_text(encoding="utf-8")

    # All three frames produce blocks
    assert "## [0:23]" in body
    assert "## [1:07]" in body
    assert "## [2:18]" in body
    # First two have text
    assert "GRANULITA VERSIO" in body
    assert "PARAMETERS" in body
    # Third frame engine returned [] → marker
    assert "_(no text detected)_" in body
    # Header counts all three
    assert "frames: 3" in body


def test_cli_missing_cache_in_batch_mode_aborts_with_hint(tmp_path, monkeypatch, capsys):
    """No frames/ at all → friendly stderr, exit 1, no ocr.md created."""
    monkeypatch.chdir(tmp_path)
    # Don't pre-create the frames dir.

    # _load_engine must NOT be called — we check cache existence first.
    sentinel = MagicMock(side_effect=AssertionError("must not load engine"))
    with patch.object(ocr_mod, "_load_engine", side_effect=sentinel):
        rc = ocr_mod.main([_URL])

    assert rc == 1
    err = capsys.readouterr().err
    assert "no cached frames" in err
    assert "yt-frames" in err  # suggests running yt-frames
    assert "--timestamps" in err  # or passing --timestamps
    assert not (tmp_path / "yt-cache" / _VID / "ocr.md").exists()


def test_cli_empty_frames_dir_aborts_with_hint(tmp_path, monkeypatch, capsys):
    """frames/ exists but is empty → same friendly abort."""
    monkeypatch.chdir(tmp_path)
    cache_root = tmp_path / "yt-cache" / _VID
    (cache_root / "frames").mkdir(parents=True)

    with patch.object(ocr_mod, "_load_engine") as load:
        rc = ocr_mod.main([_URL])

    assert rc == 1
    load.assert_not_called()
    err = capsys.readouterr().err
    assert "no cached frames" in err


def test_cli_missing_extra_prints_friendly_hint(tmp_path, monkeypatch, capsys):
    """If rapidocr is missing, the CLI surfaces the [ocr] extra install hint."""
    monkeypatch.chdir(tmp_path)
    _seed_frames(tmp_path / "yt-cache" / _VID, ["0023"])

    def _raise(_lang):
        raise OcrError(
            "yt-ocr requires the [ocr] extra:\n"
            '  pipx inject yt-tools-cli "rapidocr>=3.8,<4" "onnxruntime>=1.18"\n'
            "  # or\n"
            "  pip install 'yt-tools-cli[ocr]'"
        )

    with patch.object(ocr_mod, "_load_engine", side_effect=_raise):
        rc = ocr_mod.main([_URL])

    assert rc == 1
    err = capsys.readouterr().err
    assert 'pipx inject yt-tools-cli "rapidocr>=3.8,<4" "onnxruntime>=1.18"' in err
    assert "pip install 'yt-tools-cli[ocr]'" in err


def test_cli_language_ru_propagates_to_load_engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_frames(tmp_path / "yt-cache" / _VID, ["0023"])
    engine = _fake_engine_returning([["привет"]])

    with patch.object(ocr_mod, "_load_engine", return_value=engine) as load:
        rc = ocr_mod.main([_URL, "--language", "ru"])

    assert rc == 0
    # _load_engine called with the user-facing language string
    load.assert_called_once_with("ru")
    body = (tmp_path / "yt-cache" / _VID / "ocr.md").read_text(encoding="utf-8")
    assert "language: ru" in body
    assert "привет" in body


def test_cli_timestamps_mode_extracts_frames_first(tmp_path, monkeypatch):
    """With --timestamps, yt-ocr should invoke the internal yt-frames helper to
    populate the cache before reading frames for OCR."""
    monkeypatch.chdir(tmp_path)
    cache_root = tmp_path / "yt-cache" / _VID
    # frames/ doesn't exist yet — yt-frames is expected to create it.

    def fake_frames_run(url, out_dir=None, timestamps=None, mode="timestamps", **kwargs):
        # Simulate yt-frames writing frame_*.jpg into the cache.
        out_dir = out_dir or (cache_dir := cache_root / "frames")
        out_dir.mkdir(parents=True, exist_ok=True)
        from yt_tools.core import format_seconds_for_filename
        written = []
        for s in (timestamps or []):
            p = out_dir / f"frame_{format_seconds_for_filename(s)}.jpg"
            p.write_bytes(b"fake")
            written.append(p)
        return written

    engine = _fake_engine_returning([["CAPTURED AT 1:30"], ["CAPTURED AT 2:45"]])

    from yt_tools import frames as frames_mod

    with patch.object(frames_mod, "run", side_effect=fake_frames_run) as frun, \
         patch.object(ocr_mod, "_load_engine", return_value=engine):
        rc = ocr_mod.main([_URL, "--timestamps", "1:30,2:45"])

    assert rc == 0
    frun.assert_called_once()
    # The internal helper got both timestamps (as integer seconds)
    kwargs = frun.call_args.kwargs
    args = frun.call_args.args
    timestamps_passed = kwargs.get("timestamps") or args[2]
    assert sorted(timestamps_passed) == [90, 165]
    body = (cache_root / "ocr.md").read_text(encoding="utf-8")
    assert "CAPTURED AT 1:30" in body
    assert "CAPTURED AT 2:45" in body


def test_cli_stdout_prints_absolute_path(tmp_path, monkeypatch, capsys):
    """yt-tools stdout convention: bare absolute path of the artefact, last line."""
    monkeypatch.chdir(tmp_path)
    _seed_frames(tmp_path / "yt-cache" / _VID, ["0023"])
    engine = _fake_engine_returning([["hello"]])

    with patch.object(ocr_mod, "_load_engine", return_value=engine):
        rc = ocr_mod.main([_URL])

    assert rc == 0
    captured = capsys.readouterr()
    last_line = captured.out.strip().splitlines()[-1]
    expected = (tmp_path / "yt-cache" / _VID / "ocr.md").resolve()
    assert last_line == str(expected)


def test_cli_out_flag_writes_to_explicit_path(tmp_path, monkeypatch):
    """--out PATH overrides the default ./yt-cache/<vid>/ocr.md location."""
    monkeypatch.chdir(tmp_path)
    _seed_frames(tmp_path / "yt-cache" / _VID, ["0023"])
    engine = _fake_engine_returning([["hello"]])
    explicit = tmp_path / "custom" / "out.md"

    with patch.object(ocr_mod, "_load_engine", return_value=engine):
        rc = ocr_mod.main([_URL, "--out", str(explicit)])

    assert rc == 0
    assert explicit.exists()
    # Default path NOT written
    assert not (tmp_path / "yt-cache" / _VID / "ocr.md").exists()


def test_parse_frame_seconds_ignores_the_subsecond_part():
    # task:2782: миллисекунды в имени — часть уникальности, секунда та же.
    assert _parse_frame_seconds("frame_0023_100.jpg") == 23
    assert _parse_frame_seconds("frame_1245_012.jpg") == 12 * 60 + 45


def test_discover_frames_keeps_every_frame_of_the_same_second(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    for name in ("frame_0145.jpg", "frame_0023_900.jpg", "frame_0023.jpg", "frame_0023_100.jpg"):
        (frames / name).write_bytes(b"x")
    out = discover_frames(frames)
    assert [secs for secs, _ in out] == [23, 23, 23, 105]
    # порядок внутри секунды детерминирован (по имени, не по порядку файловой системы)
    assert [p.name for _, p in out] == [
        "frame_0023.jpg",
        "frame_0023_100.jpg",
        "frame_0023_900.jpg",
        "frame_0145.jpg",
    ]
