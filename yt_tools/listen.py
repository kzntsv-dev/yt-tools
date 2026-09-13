"""yt-listen CLI — extract WAV / spectrogram / feature-digest at given timestamps.

Two modes (mutually exclusive):
  --timestamps 1:23,4:56         (default if --timestamps is given)
  --mode interval --interval 60s

Pipeline per timestamp T:
  1. Ensure source.mp4 cache (reuse ``_ensure_source_mp4`` from frames.py).
  2. ``ffmpeg -ss T -t DURATION -ac 1 -ar SAMPLE_RATE`` → ``clip_TTTT.wav``.
  3. ``librosa.load`` → y, sr → spectral / hpss / chroma / peak features.
  4. ``bpm_detector.AudioAnalyzer.analyze_file(comprehensive=True)`` →
     tempo / key / chord progression / song-form (graceful fallback to
     librosa beat-track if the dep is unavailable).
  5. ``matplotlib`` render mel-spectrogram (log-power, viridis, ~1024×384)
     → ``spectrum_TTTT.png``.
  6. Write ``features_TTTT.md`` with required sections.

Each artifact prints ``Wrote: <abs path>`` on its own stdout line so callers
(agents using ``Read``) can grab the paths without parsing a summary.

Whisper is intentionally **not** integrated — see canonical spec
``concepts/yt-tools-audio`` section "Что НЕ в MVP".
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from yt_tools._metadata import MetadataError, fetch_video_metadata
from yt_tools.core import (
    cache_dir_for,
    extract_video_id,
    force_utf8_streams,
    format_seconds_for_filename,
    format_seconds_to_mmss,
    interval_timestamps,
    parse_timestamp_to_seconds,
)
from yt_tools.extras import require_extra
from yt_tools.frames import (
    _ensure_source_mp4,
    _format_subprocess_failure,
    _require_bin,
    parse_interval,
)

DEFAULT_DURATION_SECONDS = 30.0
DEFAULT_SAMPLE_RATE = 22050
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|m|min|h)?$", re.IGNORECASE)
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def parse_duration(spec: str) -> float:
    """Parse ``30s`` / ``1m`` / ``2h`` / bare seconds into a float number of seconds.

    Grammar parity with ``parse_interval`` — hour-scale durations are absurd for
    a per-fragment clip (default 30s) but the parser does not artificially reject them.
    """
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid duration: {spec!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "sec"):
        return value
    if unit in ("m", "min"):
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    raise ValueError(f"invalid duration unit: {unit!r}")


def parse_timestamp_to_seconds_float(ts: str) -> float:
    """Parse ``ss[.f]``, ``m:ss[.f]``, ``h:mm:ss[.f]`` to fractional seconds.

    Differs from ``yt_tools.core.parse_timestamp_to_seconds`` which floors to int —
    audio analysis benefits from sub-second precision for chord onsets etc.
    """
    parts = ts.strip().split(":")
    if len(parts) > 3:
        raise ValueError(f"too many parts in timestamp: {ts!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"non-numeric component in timestamp: {ts!r}") from e
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        m, s = nums
        return m * 60.0 + s
    h, m, s = nums
    return h * 3600.0 + m * 60.0 + s


def _ffmpeg_extract_wav(
    source: Path,
    seconds: float,
    duration: float,
    sample_rate: int,
    out_path: Path,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Extract a mono WAV slice [seconds, seconds+duration) from ``source`` at ``sample_rate``."""
    _require_bin(ffmpeg_bin)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-ss", f"{seconds:.3f}",
            "-i", str(source),
            "-t", f"{duration:.3f}",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-vn",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(_format_subprocess_failure(proc, f"ffmpeg WAV extract at {seconds}s"))


def _ffmpeg_extract_wav_streaming(
    url: str,
    seconds: float,
    duration: float,
    sample_rate: int,
    out_path: Path,
    yt_dlp_bin: str = "yt-dlp",
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Stream-extract a WAV slice via ``yt-dlp -g | ffmpeg`` (no source.mp4 cache)."""
    _require_bin(yt_dlp_bin)
    _require_bin(ffmpeg_bin)
    # Prefer the bestaudio stream — yt-listen never needs video frames.
    g = subprocess.run(
        [yt_dlp_bin, "-f", "bestaudio/best", "-g", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if g.returncode != 0:
        raise RuntimeError(_format_subprocess_failure(g, "yt-dlp -g (audio)"))
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
            "-t", f"{duration:.3f}",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-vn",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(_format_subprocess_failure(proc, f"ffmpeg streaming WAV at {seconds}s"))


def _hz_to_note(freq_hz: float) -> str:
    """Return a coarse note label like ``A4`` for an arbitrary frequency in Hz."""
    if freq_hz <= 0:
        return "?"
    import math

    midi = 69 + 12 * math.log2(freq_hz / 440.0)
    midi_round = int(round(midi))
    octave = midi_round // 12 - 1
    name = _PITCH_NAMES[midi_round % 12]
    return f"{name}{octave}"


def _librosa_features(y: Any, sr: int) -> dict[str, Any]:
    """Compute the librosa-side feature dict — wraps imports so unit tests can mock."""
    import librosa
    import numpy as np

    rms = librosa.feature.rms(y=y)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, roll_percent=0.85)[0]
    flatness = librosa.feature.spectral_flatness(y=y)[0]
    bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr)[0]
    zcr = librosa.feature.zero_crossing_rate(y=y)[0]

    # Chroma — mean intensity per pitch class.
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    chroma_order = list(np.argsort(chroma_mean)[::-1])

    # Peak frequencies — average magnitude STFT, pick top-5 bins.
    stft_mag = np.abs(librosa.stft(y))
    spec_mean = stft_mag.mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr)
    if len(spec_mean) > 5:
        peak_idx = np.argsort(spec_mean)[::-1][:5]
    else:
        peak_idx = list(range(len(spec_mean)))
    peaks = [(float(freqs[i]), float(spec_mean[i])) for i in peak_idx]

    # HPSS energy split.
    y_h, y_p = librosa.effects.hpss(y)
    e_h = float(np.sum(y_h ** 2))
    e_p = float(np.sum(y_p ** 2))
    total = e_h + e_p
    h_frac = (e_h / total) if total > 0 else 0.0
    p_frac = (e_p / total) if total > 0 else 0.0

    return {
        "rms_mean": float(rms.mean()),
        "rms_max": float(rms.max()),
        "centroid_mean": float(centroid.mean()),
        "rolloff_mean": float(rolloff.mean()),
        "bandwidth_mean": float(bandwidth.mean()),
        "flatness_mean": float(flatness.mean()),
        "zcr_mean": float(zcr.mean()),
        "chroma_mean": [float(x) for x in chroma_mean],
        "chroma_order": [int(i) for i in chroma_order],
        "peaks": peaks,
        "harmonic_fraction": h_frac,
        "percussive_fraction": p_frac,
    }


# Krumhansl-Kessler probe-tone profiles (1982). Index 0 = tonic (C).
# Major peaks at scale degrees 1, 3, 5 (indices 0, 4, 7).
# Minor peaks at scale degrees 1, ♭3, 5 (indices 0, 3, 7).
_KK_MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_KK_MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


def estimate_key_from_chroma(chroma_mean: Any) -> dict[str, Any]:
    """Estimate musical key from a 12-element mean-chroma vector.

    Returns ``{"key": "<root> <mode>", "confidence": float, "alt_key": ...,
    "alt_confidence": ..., "mode_delta": float}`` where ``alt_*`` describe the
    same-tonic candidate in the *other* mode and ``mode_delta`` =
    ``confidence`` − ``alt_confidence``. Callers may surface both candidates
    when ``mode_delta`` is small.
    """
    import numpy as np

    chroma = np.asarray(chroma_mean, dtype=float).reshape(-1)
    if chroma.shape != (12,):
        raise ValueError(f"chroma_mean must have 12 elements, got shape {chroma.shape}")

    major = np.array(_KK_MAJOR_PROFILE)
    minor = np.array(_KK_MINOR_PROFILE)

    def _cosine(a: Any, b: Any) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    scores: dict[tuple[int, str], float] = {}
    for i in range(12):
        scores[(i, "maj")] = _cosine(chroma, np.roll(major, i))
        scores[(i, "min")] = _cosine(chroma, np.roll(minor, i))

    (best_i, best_mode), best_score = max(scores.items(), key=lambda kv: kv[1])
    alt_mode = "min" if best_mode == "maj" else "maj"
    alt_score = scores[(best_i, alt_mode)]

    root = _PITCH_NAMES[best_i]
    label = f"{root} major" if best_mode == "maj" else f"{root} minor"
    alt_label = f"{root} major" if alt_mode == "maj" else f"{root} minor"

    return {
        "key": label,
        "confidence": max(best_score, 0.0),
        "alt_key": alt_label,
        "alt_confidence": max(alt_score, 0.0),
        "mode_delta": best_score - alt_score,
    }


def _librosa_fallback_basic(y: Any, sr: int) -> dict[str, Any]:
    """Compute tempo + key from raw audio when bpm_detector is unavailable.

    Wraps :func:`estimate_key_from_chroma` — the key-matching math lives
    there so it can be unit-tested without librosa.
    """
    import librosa
    import numpy as np

    tempo_arr, _ = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo_arr)[0])

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    est = estimate_key_from_chroma(chroma)
    return {
        "tempo_bpm": tempo,
        "tempo_confidence": None,
        "key": est["key"],
        "key_confidence": est["confidence"],
        "alt_key": est["alt_key"],
        "alt_confidence": est["alt_confidence"],
        "mode_delta": est["mode_delta"],
    }


def _bpm_detector_analyse(wav_path: Path, sample_rate: int) -> dict[str, Any] | None:
    """Run bpm_detector.AudioAnalyzer.analyze_file. Returns None if the dep is unavailable."""
    try:
        from bpm_detector import AudioAnalyzer  # type: ignore[import-not-found]
    except Exception:
        return None
    analyzer = AudioAnalyzer(sr=sample_rate)
    return analyzer.analyze_file(str(wav_path), detect_key=True, comprehensive=True)


def _render_spectrogram(
    y: Any,
    sr: int,
    out_path: Path,
    *,
    linear: bool = False,
) -> None:
    """Render mel-spectrogram (or linear STFT if ``linear=True``) to ``out_path`` PNG."""
    # matplotlib is only needed to *draw*: a librosa-only install can still do every
    # other yt-listen path (features, BPM/key, WAV) — refuse precisely here, where the
    # stack is actually required (D4), instead of at the entry point.
    require_extra("yt-listen --spectrogram", "audio", modules=("matplotlib",))
    import librosa
    import librosa.display  # noqa: F401  side-effect: registers display helpers
    import matplotlib

    matplotlib.use("Agg")  # headless backend — no GUI / window
    import matplotlib.pyplot as plt
    import numpy as np

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10.24, 3.84), dpi=100)  # ~1024×384
    if linear:
        S = np.abs(librosa.stft(y))
        S_db = librosa.amplitude_to_db(S, ref=np.max)
        img = librosa.display.specshow(S_db, sr=sr, x_axis="time", y_axis="log", ax=ax, cmap="viridis")
        ax.set_title("Linear STFT (log-y, dB)")
    else:
        S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128)
        S_db = librosa.power_to_db(S, ref=np.max)
        img = librosa.display.specshow(S_db, sr=sr, x_axis="time", y_axis="mel", ax=ax, cmap="viridis")
        ax.set_title("Mel-spectrogram (log-power, dB)")
    fig.colorbar(img, ax=ax, format="%+2.0f dB")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _render_chroma(y: Any, sr: int, out_path: Path) -> None:
    """Render a chromagram PNG (12 pitch classes over time)."""
    import librosa
    import librosa.display  # noqa: F401
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_path.parent.mkdir(parents=True, exist_ok=True)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    fig, ax = plt.subplots(figsize=(10.24, 3.84), dpi=100)
    img = librosa.display.specshow(chroma, sr=sr, x_axis="time", y_axis="chroma", ax=ax, cmap="viridis")
    ax.set_title("Chromagram (12 pitch classes)")
    fig.colorbar(img, ax=ax)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def _format_chroma_section(chroma_mean: list[float], chroma_order: list[int]) -> str:
    """Top-3 pitch classes as percentage of total chroma energy."""
    total = sum(chroma_mean) or 1.0
    lines = []
    for rank, idx in enumerate(chroma_order[:3], start=1):
        pct = 100.0 * chroma_mean[idx] / total
        lines.append(f"{rank}. {_PITCH_NAMES[idx]} — {pct:.1f}%")
    return "\n".join(lines)


def _format_peaks_section(peaks: list[tuple[float, float]]) -> str:
    """Top-5 spectral peaks. Sorted by descending magnitude in input."""
    lines = []
    for rank, (freq_hz, _mag) in enumerate(peaks[:5], start=1):
        note = _hz_to_note(freq_hz) if freq_hz > 20 else "?"
        lines.append(f"{rank}. {freq_hz:.0f} Hz ({note})")
    return "\n".join(lines)


def _normalise_confidence(v: Any) -> float:
    """bpm-detector's confidence fields mix 0-1 (some) and 0-100 (others) — normalise to 0-1."""
    f = float(v)
    if f > 1.0:
        return min(f / 100.0, 1.0)
    return max(f, 0.0)


def _format_chord_progression(bpm_result: dict[str, Any] | None) -> str | None:
    """Pull chord progression list out of bpm_detector result dict. Returns None if absent."""
    if not bpm_result:
        return None
    chords = bpm_result.get("chord_progression") or {}
    if isinstance(chords, dict):
        # main_progression is the cleanest shape — list[str] from bpm-detector
        # already filtered / dedup'd by the analyser. Prefer it over raw chord events.
        seq = (
            chords.get("main_progression")
            or chords.get("sequence")
            or chords.get("progression")
            or chords.get("chords")
        )
    else:
        seq = chords
    if not seq:
        return None
    # Try a few well-known shapes:
    #   - list of strings: ["Am", "F", "C", "G"]
    #   - list of dicts: [{"chord": "Am", ...}, ...]
    #   - list of tuples: [("Am", confidence, start, end), ...]  (bpm-detector's actual shape)
    if isinstance(seq, list) and seq and isinstance(seq[0], dict):
        labels = []
        for c in seq[:16]:
            label = c.get("chord") or c.get("label") or c.get("name") or "?"
            labels.append(str(label))
        return " → ".join(labels)
    if isinstance(seq, list) and seq and isinstance(seq[0], (tuple, list)):
        # First element is the chord label by convention; rest are confidence/timing.
        labels = [str(c[0]) for c in seq[:16] if len(c) > 0]
        return " → ".join(labels)
    if isinstance(seq, list):
        return " → ".join(str(c) for c in seq[:16])
    return None


def _format_structure(bpm_result: dict[str, Any] | None) -> str | None:
    """Pull song-form / sections out of bpm_detector result dict. Returns None if absent."""
    if not bpm_result:
        return None
    struct = bpm_result.get("structure") or {}
    if not isinstance(struct, dict):
        return None
    sections = struct.get("sections")
    if not sections:
        form = struct.get("form")
        return f"Form: {form}" if form else None
    lines = []
    for sec in sections[:8]:
        if isinstance(sec, dict):
            # bpm-detector uses start_time / end_time / type — fall back to start / end / label.
            label = sec.get("type") or sec.get("label") or sec.get("section") or sec.get("name") or "?"
            start = sec.get("start_time")
            if start is None:
                start = sec.get("start") or sec.get("begin") or 0.0
            end = sec.get("end_time")
            if end is None:
                end = sec.get("end") or sec.get("stop") or 0.0
            lines.append(f"- {label}: {float(start):.1f}s → {float(end):.1f}s")
        else:
            lines.append(f"- {sec}")
    return "\n".join(lines) if lines else None


# Threshold below which the primary detector's mode call is no longer trusted
# on its own — cross-check with Krumhansl-Schmuckler fallback is surfaced.
_LOW_CONF_KEY_THRESHOLD = 0.5
# Mode-discrimination margin below which Krumhansl considers the call ambiguous.
_LOW_DISCRIMINATION_DELTA = 0.05


def _format_key_field(
    *,
    bpm_result: dict[str, Any] | None,
    fallback_basic: dict[str, Any] | None,
    cross_check: dict[str, Any] | None,
) -> str:
    """Render the ``Key:`` line, augmenting with a Krumhansl cross-check when the
    primary detector confidence is low.

    Decision tree:
      1. bpm-detector path with key_confidence ≥ 0.5 → trust, render as before.
      2. bpm-detector path with key_confidence < 0.5 → render primary call AND
         append " — Krumhansl cross-check: <X>" if ``cross_check`` is provided.
         When the cross-check itself shows mode_delta < 0.05, render its alt
         mode too as "X major / X minor (low discrimination)".
      3. Fallback-only path (no bpm-detector) → render the fallback call;
         when its mode_delta < 0.05 also show the alt mode.
      4. Nothing available → "n/a".
    """
    if bpm_result and isinstance(bpm_result.get("basic_info"), dict):
        basic = bpm_result["basic_info"]
        key = basic.get("key")
        key_conf = basic.get("key_confidence")
        primary = str(key) if key else "n/a"
        if key_conf is not None:
            conf01 = _normalise_confidence(key_conf)
            primary += f" (confidence {conf01:.2f})"
            if conf01 < _LOW_CONF_KEY_THRESHOLD and cross_check is not None:
                cc_key = cross_check["key"]
                cc_delta = cross_check["mode_delta"]
                if cc_delta < _LOW_DISCRIMINATION_DELTA:
                    cc_alt = cross_check["alt_key"]
                    primary += f" — Krumhansl cross-check: {cc_key} / {cc_alt} (low discrimination, mode_delta {cc_delta:.3f})"
                else:
                    primary += f" — Krumhansl cross-check: {cc_key} (cosine {cross_check['confidence']:.2f})"
        return primary

    if fallback_basic:
        base = f"{fallback_basic['key']} (cosine {fallback_basic['key_confidence']:.2f}, librosa fallback)"
        delta = fallback_basic.get("mode_delta")
        if delta is not None and delta < _LOW_DISCRIMINATION_DELTA:
            alt = fallback_basic.get("alt_key")
            if alt:
                base += f" — alt {alt} (low discrimination, mode_delta {delta:.3f})"
        return base

    return "n/a"


def format_features_markdown(
    *,
    timestamp_seconds: float,
    duration_seconds: float,
    librosa_features: dict[str, Any],
    bpm_result: dict[str, Any] | None,
    fallback_basic: dict[str, Any] | None,
    cross_check_key: dict[str, Any] | None = None,
) -> str:
    """Compose ``features_TTTT.md`` content. All required sections present (NA where missing).

    ``cross_check_key`` — optional Krumhansl-Schmuckler estimate from the same
    chroma vector, used to augment the bpm-detector key call when its
    confidence is low. Shape: ``{"key", "confidence", "alt_key", "alt_confidence", "mode_delta"}``.
    """
    mmss = format_seconds_to_mmss(timestamp_seconds)
    lines: list[str] = [f"# Audio features @ {mmss} (duration {duration_seconds:.0f}s)", ""]

    # Tempo
    if bpm_result and isinstance(bpm_result.get("basic_info"), dict):
        basic = bpm_result["basic_info"]
        tempo = basic.get("bpm")
        tempo_conf = basic.get("bpm_confidence") or basic.get("confidence")
        tempo_str = f"{float(tempo):.1f} BPM" if tempo is not None else "n/a"
        if tempo_conf is not None:
            tempo_str += f" (confidence {_normalise_confidence(tempo_conf):.2f})"
    elif fallback_basic:
        tempo_str = f"{fallback_basic['tempo_bpm']:.1f} BPM (librosa beat-track, no confidence)"
    else:
        tempo_str = "n/a"

    key_str = _format_key_field(
        bpm_result=bpm_result,
        fallback_basic=fallback_basic,
        cross_check=cross_check_key,
    )
    lines += [
        "## Tempo + key",
        f"- **Tempo:** {tempo_str}",
        f"- **Key:** {key_str}",
        "",
    ]

    # Chord progression
    lines += ["## Chord progression"]
    chord_str = _format_chord_progression(bpm_result)
    lines += [chord_str if chord_str else "_n/a — bpm-detector unavailable or no progression found_", ""]

    # Structure
    lines += ["## Structure"]
    struct_str = _format_structure(bpm_result)
    lines += [struct_str if struct_str else "_n/a — bpm-detector unavailable or no structure found_", ""]

    # Spectral features (librosa)
    f = librosa_features
    lines += [
        "## Spectral features (librosa)",
        f"- **RMS energy:** {f['rms_mean']:.4f} mean, {f['rms_max']:.4f} max",
        f"- **Spectral centroid:** {f['centroid_mean']:.0f} Hz mean",
        f"- **Spectral bandwidth:** {f['bandwidth_mean']:.0f} Hz mean",
        f"- **Spectral rolloff (85%):** {f['rolloff_mean']:.0f} Hz",
        f"- **Spectral flatness:** {f['flatness_mean']:.3f} (tonal↔noise)",
        f"- **Zero-crossing rate:** {f['zcr_mean']:.4f}",
        "",
        "### Chroma profile (top-3 pitch classes)",
        _format_chroma_section(f["chroma_mean"], f["chroma_order"]),
        "",
    ]

    # Peak frequencies
    lines += [
        "## Peak frequencies (top-5, mean over window)",
        _format_peaks_section(f["peaks"]),
        "",
    ]

    # HPSS split
    lines += [
        "## Harmonic / percussive split",
        f"- Harmonic energy: {100.0 * f['harmonic_fraction']:.0f}%",
        f"- Percussive energy: {100.0 * f['percussive_fraction']:.0f}%",
        "",
    ]
    return "\n".join(lines)


def run(
    url: str,
    *,
    out_dir: Path | None = None,
    timestamps: list[float] | None = None,
    mode: str = "timestamps",
    interval: float | None = None,
    duration: float = DEFAULT_DURATION_SECONDS,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    no_cache_source: bool = False,
    no_wav: bool = False,
    no_spectrogram: bool = False,
    linear: bool = False,
    chroma: bool = False,
) -> list[Path]:
    """Extract audio fragments + render features per the chosen mode.

    Returns the list of written file paths (in stdout-print order). Any artifact
    creation that fails for one timestamp raises immediately — partial output for
    earlier timestamps remains on disk.
    """
    extract_video_id(url)  # validate

    if out_dir is None:
        out_dir = cache_dir_for(url) / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_root = out_dir.parent

    # The caller's own arguments are validated first: a bad command must not be
    # answered with "install an extra" (D4). Then the environment — librosa is
    # needed by every path, and the refusal must come before any download.
    if mode not in ("timestamps", "interval"):
        raise ValueError(f"unknown mode: {mode!r}")
    if mode == "timestamps" and not timestamps:
        raise ValueError("mode=timestamps requires --timestamps")
    if mode == "interval" and not interval:
        raise ValueError("mode=interval requires --interval")

    require_extra("yt-listen", "audio", modules=("librosa",))

    if mode == "timestamps":
        seconds_list = list(timestamps)  # type: ignore[arg-type]
    else:
        meta = fetch_video_metadata(url)
        seconds_list = interval_timestamps(meta["duration"], interval)  # type: ignore[arg-type]

    if not seconds_list:
        return []

    source: Path | None = None
    if not no_cache_source:
        source = _ensure_source_mp4(url, cache_root / "source.mp4")

    # Lazy-import librosa once — first import is expensive (numba JIT).
    import librosa

    written: list[Path] = []

    for s in seconds_list:
        stamp = format_seconds_for_filename(s)
        wav_path = out_dir / f"clip_{stamp}.wav"
        spec_path = out_dir / f"spectrum_{stamp}.png"
        chroma_path = out_dir / f"chroma_{stamp}.png"
        md_path = out_dir / f"features_{stamp}.md"

        # Always extract the WAV — bpm_detector + librosa both need a file/array.
        if source is not None:
            _ffmpeg_extract_wav(source, s, duration, sample_rate, wav_path)
        else:
            _ffmpeg_extract_wav_streaming(url, s, duration, sample_rate, wav_path)

        # Load it once, run all librosa-side features.
        y, sr = librosa.load(str(wav_path), sr=sample_rate, mono=True)
        feats = _librosa_features(y, sr)
        bpm_result = _bpm_detector_analyse(wav_path, sample_rate)
        fallback = _librosa_fallback_basic(y, sr) if bpm_result is None else None

        # When bpm-detector is the primary source but its key call is uncertain,
        # run a cheap Krumhansl-Schmuckler estimate on the chroma we already
        # computed and surface it as a cross-check (see format_features_markdown).
        cross_check: dict[str, Any] | None = None
        if bpm_result is not None and isinstance(bpm_result.get("basic_info"), dict):
            bd_conf = bpm_result["basic_info"].get("key_confidence")
            if bd_conf is not None and _normalise_confidence(bd_conf) < _LOW_CONF_KEY_THRESHOLD:
                cross_check = estimate_key_from_chroma(feats["chroma_mean"])

        # Spectrogram (mel or linear).
        if not no_spectrogram:
            _render_spectrogram(y, sr, spec_path, linear=linear)

        # Chroma — bonus.
        if chroma:
            _render_chroma(y, sr, chroma_path)

        # Features markdown.
        md = format_features_markdown(
            timestamp_seconds=s,
            duration_seconds=duration,
            librosa_features=feats,
            bpm_result=bpm_result,
            fallback_basic=fallback,
            cross_check_key=cross_check,
        )
        md_path.write_text(md, encoding="utf-8")

        # Now print in a stable order: WAV → spectrogram → chroma → features.
        if not no_wav:
            written.append(wav_path.resolve())
            print(f"Wrote: {wav_path.resolve()}")
        else:
            # WAV always created, but if --no-wav set we delete it after analysis.
            try:
                wav_path.unlink()
            except OSError:
                pass

        if not no_spectrogram:
            written.append(spec_path.resolve())
            print(f"Wrote: {spec_path.resolve()}")

        if chroma:
            written.append(chroma_path.resolve())
            print(f"Wrote: {chroma_path.resolve()}")

        written.append(md_path.resolve())
        print(f"Wrote: {md_path.resolve()}")

    return written


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="yt-listen",
        description="Extract WAV / spectrogram / feature-digest from YouTube audio at given timestamps.",
    )
    parser.add_argument("url", help="YouTube URL or bare video id")
    parser.add_argument("--out", type=Path, default=None, help="Output dir (default: ./yt-cache/<vid>/audio/)")
    parser.add_argument(
        "--timestamps",
        default=None,
        help="Comma-separated m:ss list (e.g. 1:23,4:56). Fractional seconds OK: 1:23.5",
    )
    parser.add_argument(
        "--mode",
        choices=["timestamps", "interval"],
        default=None,
        help="Extraction mode (auto: 'timestamps' if --timestamps given, else required).",
    )
    parser.add_argument("--interval", default=None, help="For --mode interval. Example: 30s / 1m / 2h")
    parser.add_argument(
        "--duration",
        default=f"{DEFAULT_DURATION_SECONDS:.0f}s",
        help=f"Per-fragment duration (default {DEFAULT_DURATION_SECONDS:.0f}s). Example: 10s / 1m / 1h",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help=f"Resample to N Hz mono for analysis (default {DEFAULT_SAMPLE_RATE}).",
    )
    parser.add_argument("--no-cache-source", action="store_true", help="Stream via yt-dlp -g | ffmpeg instead.")
    parser.add_argument("--no-wav", action="store_true", help="Skip WAV artifact (analyse then delete).")
    parser.add_argument("--no-spectrogram", action="store_true", help="Skip mel-spectrogram PNG.")
    parser.add_argument("--linear", action="store_true", help="Linear STFT spectrogram (default: mel + log-power).")
    parser.add_argument("--chroma", action="store_true", help="Also emit a chromagram PNG.")

    args = parser.parse_args(argv)

    mode = args.mode or ("timestamps" if args.timestamps else None)
    if mode is None:
        parser.error("must give --timestamps or --mode interval")

    timestamps: list[float] | None = None
    if args.timestamps:
        try:
            timestamps = [parse_timestamp_to_seconds_float(t) for t in args.timestamps.split(",") if t.strip()]
        except ValueError as e:
            parser.error(str(e))

    interval_seconds: float | None = None
    if args.interval:
        try:
            interval_seconds = parse_interval(args.interval)
        except ValueError as e:
            parser.error(str(e))

    try:
        duration = parse_duration(args.duration)
    except ValueError as e:
        parser.error(str(e))

    try:
        run(
            args.url,
            out_dir=args.out,
            timestamps=timestamps,
            mode=mode,
            interval=interval_seconds,
            duration=duration,
            sample_rate=args.sample_rate,
            no_cache_source=args.no_cache_source,
            no_wav=args.no_wav,
            no_spectrogram=args.no_spectrogram,
            linear=args.linear,
            chroma=args.chroma,
        )
    except (ValueError, RuntimeError, MetadataError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
