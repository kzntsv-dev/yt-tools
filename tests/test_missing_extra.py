"""Missing-extra refusal — one wording, one exact command, rc ≠ 0 ([[requirements:46]] D4, AC2, AC3).

A flow whose extra is absent must fail *named*: the reason, the command that
fixes it, and a non-zero exit code. Never an ImportError raised from the depths
of a third-party stack, and never a silent quality drop.

The "with the extra installed it works" half of AC2/AC3 is verified by hand in
a real venv (see the task report) — these tests cover the refusal itself, the
ordering (refuse before paying for a download), and the degradable path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from yt_tools import extras
from yt_tools import frames as frames_mod
from yt_tools import listen as listen_mod
from yt_tools import ocr as ocr_mod
from yt_tools import watch as watch_mod

URL = "https://youtu.be/abcDEF12345"


def _poison(monkeypatch, *module_names: str) -> None:
    """Make ``module_names`` un-importable the way a missing module is (``sys.modules`` → None)."""
    for name in module_names:
        monkeypatch.setitem(sys.modules, name, None)


def _only_importable(monkeypatch, *allowed: str) -> None:
    """Fake the import probe: ``allowed`` import, everything else raises ModuleNotFoundError."""

    def fake_import(name, *args, **kwargs):
        if name in allowed:
            return object()
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(extras, "import_module", fake_import)


def _explode(*args, **kwargs):
    raise AssertionError("network/ffmpeg work started before the missing extra was reported")


# ---- one wording, one source of truth ---------------------------------------


# [test-modify: test_frames_refusal_wording_is_exact: was
#  '  pipx inject yt-tools-cli scenedetect opencv-python'; is the same line with
#  each spec double-quoted; reason: task:2831 — the hint now formats specs
#  through one helper (extras.inject_command_for) so a bounded spec like the OCR
#  pin cannot be re-joined unquoted by a caller; `"` is required for those
#  bounds, and mixed quoting per extra would be worse than quoting all.]
def test_frames_refusal_wording_is_exact():
    assert extras.format_missing_extra("yt-frames --mode scene", "frames") == (
        "yt-frames --mode scene requires the [frames] extra:\n"
        '  pipx inject yt-tools-cli "scenedetect" "opencv-python"\n'
        "  # or\n"
        "  pip install 'yt-tools-cli[frames]'"
    )


def test_audio_refusal_wording_is_exact():
    assert extras.format_missing_extra("yt-listen", "audio") == (
        "yt-listen requires the [audio] extra:\n"
        '  pipx inject yt-tools-cli "librosa" "matplotlib"\n'
        "  # or\n"
        "  pip install 'yt-tools-cli[audio]'"
    )


# [test-modify: test_ocr_refusal_wording_is_exact: was
#  '  pipx inject yt-tools-cli rapidocr onnxruntime'; is the same line with the
#  bound specs, each double-quoted; reason: task:2831 — the injected spec now
#  carries the `>=3.8,<3.9` pin (an inject bypasses the extra's metadata, so an
#  unbounded `rapidocr` there installed the broken 3.9.x), and quotes are
#  required because `>`/`<` are redirections in bash, cmd.exe and PowerShell.]
def test_ocr_refusal_wording_is_exact():
    assert extras.format_missing_extra("yt-ocr", "ocr") == (
        "yt-ocr requires the [ocr] extra:\n"
        '  pipx inject yt-tools-cli "rapidocr>=3.8,<3.9" "onnxruntime>=1.18"\n'
        "  # or\n"
        "  pip install 'yt-tools-cli[ocr]'"
    )


def test_unknown_extra_is_named_not_a_key_error():
    with pytest.raises(ValueError) as exc_info:
        extras.format_missing_extra("yt-frames", "frame")  # typo
    assert "unknown extra: 'frame'" in str(exc_info.value)
    assert "frames" in str(exc_info.value)


def test_ocr_error_message_comes_from_the_shared_formatter(monkeypatch):
    """`yt-ocr` already refused this way — now it refuses with the shared wording."""
    _poison(monkeypatch, "rapidocr")
    with pytest.raises(ocr_mod.OcrError) as exc_info:
        ocr_mod._load_engine("en")
    assert str(exc_info.value) == extras.format_missing_extra("yt-ocr", "ocr")


# [test-modify: test_ocr_refuses_when_only_onnxruntime_is_missing: was
#  `_only_importable(monkeypatch, "rapidocr")`; is `_poison(monkeypatch,
#  "onnxruntime")`; reason: task:2831 — `_only_importable` patches
#  `extras.import_module`, which `_load_engine` never calls, so the old form only
#  passed because rapidocr itself was absent from the venv and the *first*
#  import raised. With `[ocr]` installed (now the case in CI, and the only way
#  tests/test_ocr_engine.py can run) the real engine got built here — a model
#  download inside a unit test, and on rapidocr 3.9 a ValueError instead of the
#  named refusal. Poisoning the second half of the pair tests what the test
#  says it tests.]
def test_ocr_refuses_when_only_onnxruntime_is_missing(monkeypatch):
    """The [ocr] extra is a pair: a half-installed one must not fail later, unnamed."""
    _poison(monkeypatch, "onnxruntime")
    with pytest.raises(ocr_mod.OcrError) as exc_info:
        ocr_mod._load_engine("en")
    assert str(exc_info.value) == extras.format_missing_extra("yt-ocr", "ocr")


def test_require_extra_raises_the_shared_message(monkeypatch):
    _poison(monkeypatch, "librosa")
    with pytest.raises(extras.MissingExtra) as exc_info:
        extras.require_extra("yt-listen", "audio")
    assert str(exc_info.value) == extras.format_missing_extra("yt-listen", "audio")


def test_poisoned_sys_modules_entry_counts_as_missing(monkeypatch):
    """The repo's existing trick (`sys.modules[name] = None`) must trip the probe too."""
    _poison(monkeypatch, "librosa")
    assert extras.module_missing("librosa") is True


def test_broken_install_counts_as_missing(monkeypatch):
    """A spec on disk is not availability: the check must import, not look.

    A present-but-broken heavy stack (missing native library, ABI drift) is the
    exact case that used to pass a `find_spec` probe and then explode deep inside
    the flow — the deep ImportError this module exists to replace.
    """
    monkeypatch.setattr(extras, "import_module", _raising_import(ImportError("dll load failed")))
    assert extras.module_missing("cv2") is True


def _raising_import(exc: BaseException):
    def fake_import(name, *args, **kwargs):
        raise exc

    return fake_import


# ---- D6: a blocker and a degradation are worded differently -----------------


def test_degradation_note_is_ascii_so_the_stream_choice_is_explicit(monkeypatch, capsys):
    """The note is printed by CLI *helper* code, so it does not lean on the calling
    CLI's ``force_utf8_streams()`` convention: the wording itself stays inside ASCII
    ([[task:2798]]). An em dash here would crash a cp866 console, and a redirected
    cp1251 stderr with it.
    """
    _poison(monkeypatch, "cv2")

    assert extras.warn_missing_extra("near-duplicate dedup", "frames", modules=("cv2",)) is True
    err = capsys.readouterr().err
    err.encode("ascii")  # must not raise
    assert "near-duplicate dedup skipped" in err


def test_degradation_note_for_audio_keeps_the_shared_wording(monkeypatch, capsys):
    """[audio] is the flow that reaches this most often (yt-listen) — same shape.

    A degradation is announced, never fatal, and still names the exact command
    ([[task:2799]] gap 5).
    """
    _poison(monkeypatch, "librosa")

    assert extras.warn_missing_extra("harmonic analysis", "audio") is True
    err = capsys.readouterr().err
    assert err.startswith("note: harmonic analysis skipped - the [audio] extra is not installed")
    assert "(continuing without it)." in err
    assert 'pipx inject yt-tools-cli "librosa" "matplotlib"' in err
    assert "pip install 'yt-tools-cli[audio]'" in err
    assert "requires the [audio] extra" not in err, "a degradation must not read as a blocker"


def test_degradation_note_is_not_the_fatal_wording(tmp_path, monkeypatch, capsys):
    _poison(monkeypatch, "cv2")
    candidates = [tmp_path / f"frame_000{i}.jpg" for i in range(3)]

    kept = frames_mod._select_frames(candidates, max_frames=None, dedup=True)

    err = capsys.readouterr().err
    assert kept == candidates, "the flow must survive without cv2"
    assert "near-duplicate dedup skipped" in err
    assert "continuing without it" in err
    assert "requires the [frames] extra" not in err, "a degradation must not read as a blocker"
    assert "pip install 'yt-tools-cli[frames]'" in err, "the exact command is still named"


# ---- AC2: yt-frames / yt-watch without [frames] ------------------------------


def test_yt_frames_scene_refuses_without_frames_extra(tmp_path, monkeypatch, capsys):
    _poison(monkeypatch, "scenedetect", "cv2")
    rc = frames_mod.main([URL, "--mode", "scene", "--out", str(tmp_path / "frames")])
    err = capsys.readouterr().err
    assert rc != 0, "a missing extra must be a non-zero exit"
    assert "requires the [frames] extra" in err
    assert 'pipx inject yt-tools-cli "scenedetect" "opencv-python"' in err
    assert "pip install 'yt-tools-cli[frames]'" in err
    assert "Traceback" not in err


def test_yt_frames_scene_refuses_before_downloading_anything(tmp_path, monkeypatch):
    _poison(monkeypatch, "scenedetect", "cv2")
    monkeypatch.setattr(frames_mod, "_ensure_source_mp4", _explode)
    with pytest.raises(extras.MissingExtra):
        frames_mod.run(URL, out_dir=tmp_path / "frames", mode="scene")


def test_yt_watch_refuses_without_frames_extra(tmp_path, monkeypatch, capsys):
    _poison(monkeypatch, "scenedetect", "cv2")
    monkeypatch.setattr(watch_mod, "_ensure_source_mp4", _explode)
    rc = watch_mod.main([URL, "--out", str(tmp_path / "watch")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "yt-watch requires the [frames] extra" in err
    assert "pip install 'yt-tools-cli[frames]'" in err


def test_yt_watch_refuses_before_fetching_metadata(tmp_path, monkeypatch):
    _poison(monkeypatch, "scenedetect", "cv2")

    def _no_metadata(*args, **kwargs):
        raise AssertionError("metadata fetched before the missing extra was reported")

    monkeypatch.setattr(watch_mod, "fetch_video_metadata", _no_metadata)
    with pytest.raises(extras.MissingExtra):
        watch_mod.run(URL, out_dir=tmp_path)


# ---- AC3: yt-listen without [audio] -----------------------------------------


def test_yt_listen_refuses_without_audio_extra(tmp_path, monkeypatch, capsys):
    _poison(monkeypatch, "librosa")
    rc = listen_mod.main([URL, "--timestamps", "0:05", "--out", str(tmp_path / "audio")])
    err = capsys.readouterr().err
    assert rc != 0
    assert "requires the [audio] extra" in err
    assert 'pipx inject yt-tools-cli "librosa" "matplotlib"' in err
    assert "pip install 'yt-tools-cli[audio]'" in err
    assert "Traceback" not in err


def test_yt_listen_refuses_before_extracting_audio(tmp_path, monkeypatch):
    _poison(monkeypatch, "librosa")
    monkeypatch.setattr(listen_mod, "_ensure_source_mp4", _explode)
    with pytest.raises(extras.MissingExtra):
        listen_mod.run(URL, out_dir=tmp_path / "audio", timestamps=[5.0])


def test_librosa_only_install_still_reaches_the_spectrogram_gate(monkeypatch):
    """matplotlib is needed to *draw*, not to run: the entry gate asks for librosa only."""
    _only_importable(monkeypatch, "librosa")
    extras.require_extra("yt-listen", "audio", modules=("librosa",))  # must not raise
    with pytest.raises(extras.MissingExtra):
        extras.require_extra("yt-listen --spectrogram", "audio", modules=("matplotlib",))


def test_render_spectrogram_refuses_without_matplotlib(tmp_path, monkeypatch):
    _poison(monkeypatch, "matplotlib")
    with pytest.raises(extras.MissingExtra) as exc_info:
        listen_mod._render_spectrogram(None, 22050, tmp_path / "spectrum.png")
    assert "yt-listen --spectrogram requires the [audio] extra" in str(exc_info.value)


# ---- the open decision: `--timestamps` / `--mode interval` are not gated -----


def test_timestamps_mode_runs_with_no_extra_at_all(tmp_path, monkeypatch, capsys):
    """An explicit list needs neither cv2 nor scenedetect: no refusal, no note."""
    _poison(monkeypatch, "scenedetect", "cv2", "librosa", "matplotlib")
    monkeypatch.setattr(frames_mod, "_ensure_source_mp4", _explode)
    written = [tmp_path / "frame_0005.jpg"]
    monkeypatch.setattr(frames_mod, "_extract_candidates", lambda *a, **k: written)

    result = frames_mod.run(
        URL,
        out_dir=tmp_path,
        timestamps=[5.0],
        mode="timestamps",
        no_cache_source=True,
    )

    err = capsys.readouterr().err
    assert result == written
    assert "extra" not in err, "explicit timestamps must not mention extras at all"


def test_interval_mode_degrades_instead_of_refusing(tmp_path, monkeypatch, capsys):
    """No cv2: the frames are still written, and the lost dedup is announced."""
    _poison(monkeypatch, "cv2")
    monkeypatch.setattr(frames_mod, "fetch_video_metadata", lambda *a, **k: {"duration": 180.0})
    monkeypatch.setattr(frames_mod, "_ensure_source_mp4", lambda *a, **k: tmp_path / "source.mp4")
    written = [tmp_path / f"frame_{i:04d}.jpg" for i in range(3)]
    monkeypatch.setattr(frames_mod, "_extract_candidates", lambda *a, **k: written)

    result = frames_mod.run(
        URL,
        out_dir=tmp_path,
        mode="interval",
        interval=60.0,
        max_frames=None,
        dedup=True,
    )

    err = capsys.readouterr().err
    assert result == written
    assert "near-duplicate dedup skipped" in err


# ---- the refusal must not swallow unrelated failures -------------------------


def test_unrelated_runtime_error_is_still_reported(tmp_path, monkeypatch, capsys):
    """The extra machinery must not become a catch-all that hides real bugs."""
    def _boom(*args, **kwargs):
        raise RuntimeError("ffmpeg exploded for an unrelated reason")

    monkeypatch.setattr(frames_mod, "_extract_candidates", _boom)
    rc = frames_mod.main([URL, "--timestamps", "0:05", "--out", str(tmp_path), "--no-cache-source"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "ffmpeg exploded for an unrelated reason" in err
    assert "extra" not in err
