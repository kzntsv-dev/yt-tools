"""Tests for yt-listen — pipeline smoke (mocked) + unit tests for parsers/formatters."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yt_tools import listen as listen_mod


# --------------------------------------------------------------------------- #
# Unit: timestamp parsing                                                     #
# --------------------------------------------------------------------------- #

class TestParseTimestampToSecondsFloat:
    def test_bare_seconds(self):
        assert listen_mod.parse_timestamp_to_seconds_float("45") == 45.0

    def test_mmss_int(self):
        assert listen_mod.parse_timestamp_to_seconds_float("1:23") == 83.0

    def test_mmss_fractional(self):
        assert listen_mod.parse_timestamp_to_seconds_float("1:23.5") == 83.5

    def test_hhmmss(self):
        assert listen_mod.parse_timestamp_to_seconds_float("1:02:03") == 3723.0

    def test_hhmmss_fractional(self):
        assert listen_mod.parse_timestamp_to_seconds_float("0:00:10.25") == 10.25

    def test_too_many_parts(self):
        with pytest.raises(ValueError, match=r"too many parts"):
            listen_mod.parse_timestamp_to_seconds_float("1:2:3:4")

    def test_non_numeric(self):
        with pytest.raises(ValueError, match=r"non-numeric"):
            listen_mod.parse_timestamp_to_seconds_float("a:b")


# --------------------------------------------------------------------------- #
# Unit: duration parsing                                                      #
# --------------------------------------------------------------------------- #

class TestParseDuration:
    def test_bare_seconds(self):
        assert listen_mod.parse_duration("30") == 30.0

    def test_seconds_suffix(self):
        assert listen_mod.parse_duration("10s") == 10.0

    def test_minutes_suffix(self):
        assert listen_mod.parse_duration("2m") == 120.0

    def test_hours_suffix(self):
        assert listen_mod.parse_duration("1h") == 3600.0

    def test_fractional(self):
        assert listen_mod.parse_duration("0.5s") == 0.5

    def test_invalid(self):
        with pytest.raises(ValueError, match=r"invalid duration"):
            listen_mod.parse_duration("forever")


# --------------------------------------------------------------------------- #
# Unit: feature-formatting (markdown structure)                               #
# --------------------------------------------------------------------------- #

def _fake_librosa_features():
    return {
        "rms_mean": 0.142,
        "rms_max": 0.187,
        "centroid_mean": 2840.0,
        "rolloff_mean": 6200.0,
        "bandwidth_mean": 1800.0,
        "flatness_mean": 0.21,
        "zcr_mean": 0.094,
        # chroma_mean: 12 values, A=index 9 highest.
        "chroma_mean": [0.05, 0.04, 0.06, 0.03, 0.07, 0.04, 0.05, 0.06, 0.04, 0.18, 0.05, 0.06],
        "chroma_order": [9, 4, 7, 2, 11, 8, 6, 0, 10, 5, 1, 3],
        "peaks": [(110.0, 1.5), (220.0, 1.2), (440.0, 0.9), (2840.0, 0.7), (8200.0, 0.4)],
        "harmonic_fraction": 0.64,
        "percussive_fraction": 0.36,
    }


def _fake_bpm_result():
    return {
        "basic_info": {"bpm": 128.0, "bpm_confidence": 0.87, "key": "A minor", "key_confidence": 0.91},
        "chord_progression": {"chords": ["Am", "F", "C", "G"]},
        "structure": {"sections": [
            {"label": "intro", "start": 0.0, "end": 8.0},
            {"label": "verse", "start": 8.0, "end": 30.0},
        ]},
    }


class TestFormatFeaturesMarkdown:
    def test_all_required_sections_present_with_bpm_detector(self):
        md = listen_mod.format_features_markdown(
            timestamp_seconds=150.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=_fake_bpm_result(),
            fallback_basic=None,
        )
        # Header with mm:ss
        assert "# Audio features @ 2:30 (duration 30s)" in md
        # All six required sections
        assert "## Tempo + key" in md
        assert "## Chord progression" in md
        assert "## Structure" in md
        assert "## Spectral features (librosa)" in md
        assert "## Peak frequencies" in md
        assert "## Harmonic / percussive split" in md
        # Tempo+key values surfaced
        assert "128.0 BPM" in md
        assert "A minor" in md
        # Chord progression rendered as arrow-chain
        assert "Am → F → C → G" in md
        # Structure rendered
        assert "intro" in md and "verse" in md
        # Spectral values
        assert "2840 Hz" in md
        # Top-3 chroma — A should be ranked first
        assert "1. A —" in md
        # Peaks
        assert "110 Hz" in md and "A2" in md  # note label rendered
        # HPSS split
        assert "Harmonic energy: 64%" in md
        assert "Percussive energy: 36%" in md

    def test_high_conf_bpm_detector_no_cross_check_shown(self):
        # bpm-detector key_confidence 0.91 >= threshold → cross-check suppressed
        # even if provided.
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=_fake_bpm_result(),
            fallback_basic=None,
            cross_check_key={
                "key": "G# major", "confidence": 0.62,
                "alt_key": "G# minor", "alt_confidence": 0.58,
                "mode_delta": 0.04,
            },
        )
        assert "A minor (confidence 0.91)" in md
        assert "Krumhansl cross-check" not in md

    def test_low_conf_bpm_detector_appends_cross_check(self):
        # The NGGYU regression: bpm-detector says G# Minor 0.40, our Krumhansl
        # cross-check has narrow mode_delta → render combined "X / Y (low discrimination)".
        bpm = {
            "basic_info": {"bpm": 113.5, "bpm_confidence": 1.00,
                           "key": "G# Minor", "key_confidence": 0.40},
            "chord_progression": {"main_progression": ["G#", "D#", "G#"]},
            "structure": {"sections": []},
        }
        cross = {
            "key": "G# major", "confidence": 0.78,
            "alt_key": "G# minor", "alt_confidence": 0.76,
            "mode_delta": 0.02,
        }
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=bpm,
            fallback_basic=None,
            cross_check_key=cross,
        )
        # Primary call still shown.
        assert "G# Minor (confidence 0.40)" in md
        # Cross-check rendered with both candidates because mode_delta < 0.05.
        assert "Krumhansl cross-check" in md
        assert "G# major / G# minor" in md
        assert "low discrimination" in md

    def test_low_conf_bpm_detector_cross_check_high_discrimination(self):
        # bpm-detector low conf, but Krumhansl is confident → show its single call.
        bpm = {
            "basic_info": {"bpm": 113.5, "bpm_confidence": 1.00,
                           "key": "G# Minor", "key_confidence": 0.40},
        }
        cross = {
            "key": "A♭ major", "confidence": 0.85,
            "alt_key": "A♭ minor", "alt_confidence": 0.55,
            "mode_delta": 0.30,
        }
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=bpm,
            fallback_basic=None,
            cross_check_key=cross,
        )
        assert "G# Minor (confidence 0.40)" in md
        assert "Krumhansl cross-check: A♭ major (cosine 0.85)" in md
        # No combined rendering when delta is healthy.
        assert "low discrimination" not in md

    def test_fallback_only_low_discrimination_shows_alt(self):
        # No bpm-detector. Fallback's own mode_delta is narrow → render alt too.
        fallback = {
            "tempo_bpm": 120.0,
            "tempo_confidence": None,
            "key": "G# minor",
            "key_confidence": 0.65,
            "alt_key": "G# major",
            "alt_confidence": 0.63,
            "mode_delta": 0.02,
        }
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=None,
            fallback_basic=fallback,
        )
        assert "G# minor (cosine 0.65, librosa fallback)" in md
        assert "alt G# major" in md
        assert "low discrimination" in md

    def test_fallback_without_alt_fields_backcompat(self):
        # Fallback dict without alt_key/mode_delta keys still renders cleanly.
        fallback = {
            "tempo_bpm": 120.0,
            "tempo_confidence": None,
            "key": "C major",
            "key_confidence": 0.85,
        }
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=30.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=None,
            fallback_basic=fallback,
        )
        assert "C major (cosine 0.85, librosa fallback)" in md

    def test_required_sections_present_with_fallback(self):
        # Sections still rendered even when bpm_detector dep is unavailable.
        md = listen_mod.format_features_markdown(
            timestamp_seconds=30.0,
            duration_seconds=10.0,
            librosa_features=_fake_librosa_features(),
            bpm_result=None,
            fallback_basic={"tempo_bpm": 120.0, "tempo_confidence": None, "key": "C major", "key_confidence": 0.85},
        )
        assert "## Tempo + key" in md
        assert "## Chord progression" in md
        assert "## Structure" in md
        assert "## Spectral features (librosa)" in md
        assert "## Peak frequencies" in md
        assert "## Harmonic / percussive split" in md
        # Fallback tempo + key surfaced
        assert "120.0 BPM" in md
        assert "C major" in md
        # n/a markers for absent bpm-detector sections
        assert "_n/a" in md  # at least once for chord progression and structure


class TestEstimateKeyFromChroma:
    """Krumhansl-Schmuckler key estimation on synthetic chroma vectors.

    Sharp-only naming convention — ``A♭ Major`` is reported as ``G# major``.
    Enharmonic equivalents are the same pitch class, so the test asserts on
    the sharp spelling.
    """

    # The estimator itself reaches for numpy (the chroma vector's own type),
    # so this whole class is [audio] even where the input is a plain list.
    pytestmark = pytest.mark.usefixtures("audio_stack")

    def _chroma_for(self, indices_with_weights: dict[int, float], floor: float = 0.0) -> list[float]:
        """Build a 12-element chroma vector. ``floor`` adds uniform noise to non-listed bins."""
        v = [floor] * 12
        for i, w in indices_with_weights.items():
            v[i] = w
        return v

    def test_pure_c_major_triad(self):
        # C(0), E(4), G(7) — canonical C major.
        chroma = self._chroma_for({0: 1.0, 4: 1.0, 7: 1.0})
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["key"] == "C major"
        assert result["confidence"] > 0.5
        # Same-tonic alternative is C minor.
        assert result["alt_key"] == "C minor"
        assert result["mode_delta"] > 0.0  # major wins by some margin

    def test_pure_a_minor_triad(self):
        # A(9), C(0), E(4) — canonical A minor (relative minor of C major).
        chroma = self._chroma_for({9: 1.0, 0: 1.0, 4: 1.0})
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["key"] == "A minor"
        assert result["confidence"] > 0.5
        assert result["alt_key"] == "A major"

    def test_pure_a_flat_major_triad_picks_major(self):
        # A♭(8), C(0), E♭(3) — A♭ Major triad. Detector reports as 'G# major' (sharp spelling).
        # Must NOT collapse to 'G# minor' (the enharmonic-equivalent relative-minor confusion).
        chroma = self._chroma_for({8: 1.0, 0: 1.0, 3: 1.0})
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["key"] == "G# major"
        assert result["alt_key"] == "G# minor"
        assert result["mode_delta"] > 0.0

    def test_nggyu_chord_progression_shape_prefers_major(self):
        # Approximates "Never Gonna Give You Up" (true key: A♭ Major,
        # chord vocab A♭ → E♭ → Fm → D♭).
        # Aggregate pitch-class mass from those chords:
        #   A♭(8) in 3 chords → 1.0
        #   C(0)  in 2 chords → 0.7
        #   E♭(3) in 2 chords → 0.7
        #   F(5)  in 2 chords → 0.7  (M6 of A♭ Major — diatonic; NOT diatonic to G♯ Minor)
        #   D♭(1) in 1 chord  → 0.4
        #   G(7)  in 1 chord  → 0.4
        #   B♭(10) in 1 chord → 0.4
        # Other bins low noise.
        chroma = self._chroma_for(
            {8: 1.0, 0: 0.7, 3: 0.7, 5: 0.7, 1: 0.4, 7: 0.4, 10: 0.4},
            floor=0.05,
        )
        result = listen_mod.estimate_key_from_chroma(chroma)
        # Tonic is G#/A♭ either way — confirm the detector at least pins the tonic.
        assert result["key"].startswith("G#"), f"tonic should be G# (= A♭), got {result['key']!r}"
        # Acceptance: either picks Major outright, OR flags low discrimination
        # (mode_delta < 0.05) — both are acceptable per task spec option (1).
        # Hard-fail on confidently picking minor: that's the bug.
        if result["key"] == "G# minor":
            assert result["mode_delta"] < 0.05, (
                f"detector picked G# minor with mode_delta={result['mode_delta']:.4f} — "
                "must either pick G# major or flag low discrimination (delta<0.05)"
            )

    def test_zero_chroma_returns_zero_confidence(self):
        chroma = [0.0] * 12
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["confidence"] == 0.0

    def test_rejects_wrong_shape(self):
        with pytest.raises(ValueError, match=r"12 elements"):
            listen_mod.estimate_key_from_chroma([0.0] * 11)

    def test_accepts_numpy_array_input(self):
        import numpy as np
        chroma = np.zeros(12)
        chroma[0] = chroma[4] = chroma[7] = 1.0
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["key"] == "C major"

    def test_mode_delta_sign_matches_best_minus_alt(self):
        # Pure C major: best is C major, alt is C minor. mode_delta = best - alt > 0.
        chroma = self._chroma_for({0: 1.0, 4: 1.0, 7: 1.0})
        result = listen_mod.estimate_key_from_chroma(chroma)
        assert result["mode_delta"] == pytest.approx(result["confidence"] - result["alt_confidence"])


class TestFormatHelpers:
    def test_chord_progression_dict_with_sequence(self):
        assert listen_mod._format_chord_progression(
            {"chord_progression": {"sequence": ["Dm", "G7", "Cmaj7"]}}
        ) == "Dm → G7 → Cmaj7"

    def test_chord_progression_list_of_dicts(self):
        result = listen_mod._format_chord_progression({"chord_progression": [
            {"chord": "C", "start": 0, "end": 1},
            {"chord": "Am", "start": 1, "end": 2},
        ]})
        assert result == "C → Am"

    def test_chord_progression_none_when_missing(self):
        assert listen_mod._format_chord_progression(None) is None
        assert listen_mod._format_chord_progression({}) is None
        assert listen_mod._format_chord_progression({"chord_progression": []}) is None

    def test_structure_sections(self):
        result = listen_mod._format_structure({"structure": {"sections": [
            {"label": "verse", "start": 0.0, "end": 16.0},
            {"label": "chorus", "start": 16.0, "end": 32.0},
        ]}})
        assert "verse" in result and "chorus" in result
        assert "0.0s" in result and "32.0s" in result

    def test_structure_none_when_missing(self):
        assert listen_mod._format_structure(None) is None
        assert listen_mod._format_structure({}) is None

    def test_hz_to_note(self):
        # A4 = 440 Hz is the canonical anchor.
        assert listen_mod._hz_to_note(440.0) == "A4"
        # A2 = 110 Hz
        assert listen_mod._hz_to_note(110.0) == "A2"
        # 0 Hz is the unknown case
        assert listen_mod._hz_to_note(0.0) == "?"


# --------------------------------------------------------------------------- #
# Smoke: pipeline reaches each stage (ffmpeg + librosa + bpm-detector mocked) #
# --------------------------------------------------------------------------- #

def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ffmpeg"], returncode=returncode, stdout=stdout, stderr=stderr)


# `audio_stack` (numpy + librosa, or a skip with the install command) lives in
# `tests/conftest.py` since [[task:2842]] — the same fixture gates the key
# estimation class below, and one declaration has to serve both (the CI guard
# counts the tests behind it by marker). See the module docstring there.


def test_run_pipeline_smoke(tmp_path, capsys, monkeypatch, audio_stack):
    """Full pipeline with everything stubbed — verifies stdout contract + file plumbing."""
    np, librosa = audio_stack

    cache_dir = tmp_path / "yt-cache" / "dQw4w9WgXcQ"
    audio_dir = cache_dir / "audio"

    # Fake _ensure_source_mp4 — pretend the cache file exists, never run yt-dlp.
    def fake_ensure(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00" * 16)
        return dest

    # Fake ffmpeg WAV extract — touch the output file.
    def fake_wav_extract(source, seconds, duration, sample_rate, out_path, ffmpeg_bin="ffmpeg"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")  # bogus, never read

    # Fake librosa.load → return a tiny array (1 second of silence).
    def fake_load(path, sr=22050, mono=True):
        return np.zeros(sr, dtype=np.float32), sr

    monkeypatch.setattr(listen_mod, "_ensure_source_mp4", fake_ensure)
    monkeypatch.setattr(listen_mod, "_ffmpeg_extract_wav", fake_wav_extract)
    monkeypatch.setattr(listen_mod, "_librosa_features", lambda y, sr: _fake_librosa_features())
    monkeypatch.setattr(listen_mod, "_bpm_detector_analyse", lambda *a, **k: _fake_bpm_result())
    monkeypatch.setattr(listen_mod, "_render_spectrogram", lambda y, sr, out_path, **kw: out_path.write_bytes(b"\x89PNG"))
    monkeypatch.setattr(listen_mod, "_render_chroma", lambda y, sr, out_path: out_path.write_bytes(b"\x89PNG"))

    monkeypatch.setattr(librosa, "load", fake_load)

    written = listen_mod.run(
        "https://youtu.be/dQw4w9WgXcQ",
        out_dir=audio_dir,
        timestamps=[30.0, 60.0],
        mode="timestamps",
        duration=10.0,
        sample_rate=22050,
    )

    # 3 artifacts per timestamp × 2 timestamps = 6 paths.
    assert len(written) == 6
    # Every path printed on its own "Wrote: " stdout line.
    captured = capsys.readouterr().out
    wrote_lines = [line for line in captured.splitlines() if line.startswith("Wrote: ")]
    assert len(wrote_lines) == 6
    for p in written:
        assert any(str(p) in line for line in wrote_lines)
    # Every artifact exists on disk.
    for p in written:
        assert p.exists()
    # Filename stems use the canonical mmss layout.
    names = {p.name for p in written}
    assert "clip_0030.wav" in names
    assert "spectrum_0030.png" in names
    assert "features_0030.md" in names
    assert "clip_0100.wav" in names


def test_run_no_wav_deletes_file(tmp_path, capsys, monkeypatch, audio_stack):
    """--no-wav: WAV is created (needed for bpm_detector analyse) then deleted."""
    np, librosa = audio_stack

    audio_dir = tmp_path / "yt-cache" / "dQw4w9WgXcQ" / "audio"

    def fake_ensure(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00")
        return dest

    def fake_wav_extract(source, seconds, duration, sample_rate, out_path, ffmpeg_bin="ffmpeg"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")

    monkeypatch.setattr(listen_mod, "_ensure_source_mp4", fake_ensure)
    monkeypatch.setattr(listen_mod, "_ffmpeg_extract_wav", fake_wav_extract)
    monkeypatch.setattr(listen_mod, "_librosa_features", lambda y, sr: _fake_librosa_features())
    monkeypatch.setattr(listen_mod, "_bpm_detector_analyse", lambda *a, **k: _fake_bpm_result())
    monkeypatch.setattr(listen_mod, "_render_spectrogram", lambda y, sr, out_path, **kw: out_path.write_bytes(b"\x89PNG"))

    monkeypatch.setattr(librosa, "load", lambda path, sr=22050, mono=True: (np.zeros(sr, dtype=np.float32), sr))

    written = listen_mod.run(
        "https://youtu.be/dQw4w9WgXcQ",
        out_dir=audio_dir,
        timestamps=[30.0],
        mode="timestamps",
        duration=10.0,
        no_wav=True,
    )

    # WAV not in returned paths, but spectrogram + features are.
    names = {p.name for p in written}
    assert "clip_0030.wav" not in names
    assert "spectrum_0030.png" in names
    assert "features_0030.md" in names
    # WAV file deleted from disk.
    assert not (audio_dir / "clip_0030.wav").exists()


def test_bpm_detector_fallback_when_unavailable(tmp_path, capsys, monkeypatch, audio_stack):
    """When bpm_detector returns None, fallback Krumhansl-Schmuckler kicks in and markdown still has all sections."""
    np, librosa = audio_stack

    audio_dir = tmp_path / "yt-cache" / "dQw4w9WgXcQ" / "audio"

    def fake_ensure(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00")
        return dest

    def fake_wav_extract(source, seconds, duration, sample_rate, out_path, ffmpeg_bin="ffmpeg"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")

    monkeypatch.setattr(listen_mod, "_ensure_source_mp4", fake_ensure)
    monkeypatch.setattr(listen_mod, "_ffmpeg_extract_wav", fake_wav_extract)
    monkeypatch.setattr(listen_mod, "_librosa_features", lambda y, sr: _fake_librosa_features())
    monkeypatch.setattr(listen_mod, "_bpm_detector_analyse", lambda *a, **k: None)
    monkeypatch.setattr(listen_mod, "_librosa_fallback_basic", lambda y, sr: {
        "tempo_bpm": 120.0, "tempo_confidence": None, "key": "C major", "key_confidence": 0.78,
    })
    monkeypatch.setattr(listen_mod, "_render_spectrogram", lambda y, sr, out_path, **kw: out_path.write_bytes(b"\x89PNG"))

    monkeypatch.setattr(librosa, "load", lambda path, sr=22050, mono=True: (np.zeros(sr, dtype=np.float32), sr))

    listen_mod.run(
        "https://youtu.be/dQw4w9WgXcQ",
        out_dir=audio_dir,
        timestamps=[30.0],
        mode="timestamps",
        duration=10.0,
    )

    md = (audio_dir / "features_0030.md").read_text(encoding="utf-8")
    assert "120.0 BPM" in md
    assert "C major" in md
    # Required sections still present
    assert "## Tempo + key" in md
    assert "## Chord progression" in md
    assert "## Structure" in md


def test_run_rejects_empty_timestamps():
    with pytest.raises(ValueError, match=r"mode=timestamps requires --timestamps"):
        listen_mod.run("https://youtu.be/dQw4w9WgXcQ", timestamps=[], mode="timestamps")


def test_run_rejects_unknown_mode():
    with pytest.raises(ValueError, match=r"unknown mode"):
        listen_mod.run("https://youtu.be/dQw4w9WgXcQ", mode="wat", timestamps=[1.0])
