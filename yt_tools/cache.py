"""Source-mp4 cache layout: ``<cwd>/yt-cache/<video-id>/...`` — list and prune helpers."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CacheEntry:
    video_id: str
    path: Path
    size_bytes: int
    mtime: float
    #: False when part of the tree could not be walked (permission wall, vanished
    #: file): ``size_bytes`` is then a lower bound, not the size. Diagnosticians
    #: are expected to say so instead of reporting a confident number.
    complete: bool = True


def cache_root(base: Path | None = None) -> Path:
    """The cache root every flow writes to: ``<base or cwd>/yt-cache``."""
    return (base or Path.cwd()) / "yt-cache"


def _dir_size(path: Path) -> tuple[int, bool]:
    """Total bytes under ``path``, plus whether the whole tree could be walked.

    Best-effort by contract: an unreadable subtree yields a partial size and
    ``False`` instead of a confident number, because the caller is a *diagnostic*
    that exists to explain exactly that broken environment ([[requirements:46]] D5,
    [[task:2797]] F1). ``os.walk`` surfaces an unscannable directory through
    ``onerror`` and carries on; ``Path.rglob`` swallows the same
    ``PermissionError`` inside pathlib's glob machinery (verified on 3.10/3.12/3.13)
    and returns a short list — the undercount would look exactly like a right
    answer, which is the worse half of F1.
    """
    total = 0
    complete = True

    def _unreadable(_error: OSError) -> None:
        nonlocal complete
        complete = False

    for dirpath, _dirnames, filenames in os.walk(path, onerror=_unreadable):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                complete = False
    return total, complete


def cache_list(cache_root: Path) -> list[CacheEntry]:
    """List per-video subdirectories under ``cache_root``."""
    if not cache_root.exists():
        return []
    entries: list[CacheEntry] = []
    for child in sorted(cache_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        size_bytes, complete = _dir_size(child)
        entries.append(
            CacheEntry(
                video_id=child.name,
                path=child,
                size_bytes=size_bytes,
                mtime=mtime,
                complete=complete,
            )
        )
    return entries


def cache_prune(cache_root: Path, older_than_days: float) -> list[Path]:
    """Remove cache subdirs whose mtime is older than ``older_than_days``.

    Returns the list of removed paths.
    """
    if not cache_root.exists():
        return []
    cutoff = time.time() - older_than_days * 86400
    removed: list[Path] = []
    for entry in cache_list(cache_root):
        if entry.mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)
            removed.append(entry.path)
    return removed


def format_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"
