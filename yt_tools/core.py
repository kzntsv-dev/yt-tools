"""Helpers — video-id extraction, timestamp conversions, cache path layout, CLI stream config."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def force_utf8_streams() -> None:
    """Force stdout/stderr to UTF-8 so non-ASCII (yt-dlp warnings, transcript text) renders
    on Windows consoles (cp1251 default) without mojibake. No-op where reconfigure isn't
    available (wrapped streams in pytest's capsys, redirected file streams, etc.)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PATTERNS = ("/shorts/", "/embed/", "/v/", "/live/")


def extract_video_id(url_or_id: str) -> str:
    """Extract the 11-char YouTube video id from a URL or accept a bare id.

    Supports watch?v=, youtu.be/, /shorts/, /embed/, /v/, /live/ forms.
    Raises ValueError if no id can be found.
    """
    if not url_or_id:
        raise ValueError("empty url/id")

    if _VIDEO_ID_RE.match(url_or_id):
        return url_or_id

    parsed = urlparse(url_or_id)
    host = (parsed.hostname or "").lower()

    if host in ("youtu.be",):
        vid = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(vid):
            return vid

    if host.endswith("youtube.com") or host == "youtube.com":
        if parsed.path in ("/watch", "/watch/"):
            qs = parse_qs(parsed.query)
            vids = qs.get("v") or []
            if vids and _VIDEO_ID_RE.match(vids[0]):
                return vids[0]
        for pat in _PATH_PATTERNS:
            if pat in parsed.path:
                tail = parsed.path.split(pat, 1)[1].split("/")[0]
                if _VIDEO_ID_RE.match(tail):
                    return tail

    raise ValueError(f"cannot extract video id from: {url_or_id!r}")


def format_seconds_to_mmss(seconds: float) -> str:
    """Format seconds as ``m:ss`` or ``h:mm:ss`` (no zero-padded hours)."""
    s = int(seconds)
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def format_seconds_for_filename(seconds: float) -> str:
    """Format seconds as a fixed-width filename-friendly string (``mmss`` or ``hhmmss``)."""
    s = int(seconds)
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}{m:02d}{sec:02d}"
    return f"{m:02d}{sec:02d}"


def frame_filename(seconds: float) -> str:
    """Unique, timestamp-derived frame file name: ``frame_<mmss>.jpg``.

    ``format_seconds_for_filename`` keeps only whole seconds, so two timestamps inside
    one second used to collide in one file name — and ``ffmpeg -y`` silently overwrote
    the first frame (issue:74). A sub-second moment therefore carries its milliseconds:
    ``frame_0130.jpg`` for 90.0s, ``frame_0130_250.jpg`` for 90.25s.

    The name is a pure function of the timestamp — never of the run, the list order or
    what is already on disk. That is what makes ``yt-watch``'s "file already exists,
    skip extraction" cache honest: a name always means the same moment, so a later run
    with a different scene list cannot be handed last run's frame for another timestamp.

    Millisecond resolution is the identity of a moment here: two timestamps closer
    together than a millisecond are the same moment and collapse into one frame.
    """
    total_ms = max(0, round(seconds * 1000))
    whole, millis = divmod(total_ms, 1000)
    stem = f"frame_{format_seconds_for_filename(whole)}"
    return f"{stem}.jpg" if millis == 0 else f"{stem}_{millis:03d}.jpg"


def parse_timestamp_to_seconds(ts: str) -> int:
    """Parse ``ss``, ``m:ss``, or ``h:mm:ss`` into integer seconds."""
    parts = ts.strip().split(":")
    if len(parts) > 3:
        raise ValueError(f"too many parts in timestamp: {ts!r}")
    try:
        nums = [int(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"non-integer component in timestamp: {ts!r}") from e
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        m, s = nums
        return m * 60 + s
    h, m, s = nums
    return h * 3600 + m * 60 + s


def cache_dir_for(url_or_id: str, base: Path | None = None) -> Path:
    """Return ``<base>/yt-cache/<video-id>``. Does not create the directory."""
    vid = extract_video_id(url_or_id)
    root = (base or Path.cwd()) / "yt-cache" / vid
    return root


def format_count(value) -> str | None:
    """Comma-group an integer count. Returns None for missing or non-numeric input."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return None


def format_upload_date_iso(raw) -> str | None:
    """``"20240115"`` → ``"2024-01-15"``. Returns None for missing/malformed input."""
    if not raw or not isinstance(raw, str) or len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def interval_timestamps(duration_seconds: float, interval: float) -> list[float]:
    """Generate ``[0, interval, 2*interval, ...]`` up to (but not including) ``duration_seconds``.

    Returns an empty list for non-positive inputs. Shared by ``yt-frames`` and ``yt-listen``
    in their ``--mode interval`` paths.
    """
    if duration_seconds <= 0 or interval <= 0:
        return []
    out: list[float] = []
    t = 0.0
    while t < duration_seconds:
        out.append(t)
        t += interval
    return out
