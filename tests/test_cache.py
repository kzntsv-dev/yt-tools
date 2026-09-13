"""Tests for cache list/prune helpers."""

import os
import time
from pathlib import Path

import pytest

from yt_tools import cache as cache_mod
from yt_tools.cache import cache_list, cache_prune


def _touch(path: Path, mtime_offset_days: float = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if mtime_offset_days:
        ts = time.time() - mtime_offset_days * 86400
        import os
        os.utime(path, (ts, ts))
        os.utime(path.parent, (ts, ts))


def test_cache_list_empty(tmp_path: Path):
    entries = cache_list(tmp_path / "yt-cache")
    assert entries == []


def test_cache_list_finds_video_dirs(tmp_path: Path):
    cache_root = tmp_path / "yt-cache"
    _touch(cache_root / "abc12345" / "source.mp4")
    _touch(cache_root / "xyz67890" / "transcript.md")
    entries = cache_list(cache_root)
    ids = sorted(e.video_id for e in entries)
    assert ids == ["abc12345", "xyz67890"]


def test_cache_list_reports_size(tmp_path: Path):
    cache_root = tmp_path / "yt-cache"
    (cache_root / "abc12345").mkdir(parents=True)
    (cache_root / "abc12345" / "f.bin").write_bytes(b"x" * 1024)
    entries = cache_list(cache_root)
    assert len(entries) == 1
    assert entries[0].size_bytes == 1024  # exact: a `>=` here passes on an inflated number


def test_cache_root_is_the_base_plus_the_yt_cache_dir(tmp_path: Path):
    """The layout every flow writes to — and the path `--base` routes through."""
    assert cache_mod.cache_root(tmp_path) == tmp_path / "yt-cache"
    assert cache_mod.cache_root() == Path.cwd() / "yt-cache"


def test_cache_prune_nothing_when_young(tmp_path: Path):
    cache_root = tmp_path / "yt-cache"
    _touch(cache_root / "fresh" / "source.mp4", mtime_offset_days=1)
    pruned = cache_prune(cache_root, older_than_days=7)
    assert pruned == []
    assert (cache_root / "fresh").exists()


def test_cache_prune_removes_old_dirs(tmp_path: Path):
    cache_root = tmp_path / "yt-cache"
    _touch(cache_root / "old" / "source.mp4", mtime_offset_days=14)
    _touch(cache_root / "fresh" / "source.mp4", mtime_offset_days=1)
    pruned = cache_prune(cache_root, older_than_days=7)
    assert [p.name for p in pruned] == ["old"]
    assert not (cache_root / "old").exists()
    assert (cache_root / "fresh").exists()


def test_cache_prune_missing_root_is_noop(tmp_path: Path):
    pruned = cache_prune(tmp_path / "no-such-dir", older_than_days=7)
    assert pruned == []


def _deny_reads(monkeypatch, blocked: Path):
    """Deny directory reads; both spellings, because the stdlib moved from
    ``os.listdir`` (3.12 and older) to ``os.scandir`` (3.13+) in ``iterdir``."""
    real_scandir, real_listdir = os.scandir, os.listdir

    def exploding_scandir(path):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    def exploding_listdir(path="."):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(cache_mod.os, "scandir", exploding_scandir)
    monkeypatch.setattr(cache_mod.os, "listdir", exploding_listdir)


def test_dir_size_never_propagates_a_filesystem_error(monkeypatch, tmp_path: Path):
    """The contract of ``_dir_size``: a wall yields a partial size, never a raise.

    This is the subdir half of F1 — the walk hitting ``PermissionError`` must not
    become the caller's traceback *or* a silent undercount. Injected at the scan
    call because Windows cannot produce the wall with chmod (F4); the real-wall
    variants are the POSIX tests in ``test_doctor.py``.
    """
    cache_root = tmp_path / "yt-cache"
    blocked = cache_root / "abc12345" / "sub"
    blocked.mkdir(parents=True)
    (blocked / "source.mp4").write_bytes(b"x" * 1024)
    _deny_reads(monkeypatch, blocked)

    size, complete = cache_mod._dir_size(cache_root / "abc12345")
    assert complete is False
    assert size == 0  # the wall is the only thing under it here


def test_cache_list_marks_an_unreadable_video_dir_as_incomplete(monkeypatch, tmp_path: Path):
    """Size is best-effort: an unreadable subtree must not raise, only be flagged."""
    cache_root = tmp_path / "yt-cache"
    blocked = cache_root / "abc12345" / "sub"
    blocked.mkdir(parents=True)
    (blocked / "source.mp4").write_bytes(b"x" * 1024)
    (cache_root / "abc12345" / "other.mp4").write_bytes(b"y" * 512)
    _deny_reads(monkeypatch, blocked)

    (entry,) = cache_list(cache_root)
    assert entry.complete is False
    assert entry.size_bytes == 512  # the readable part, as a lower bound


def test_cache_list_reports_a_fully_walked_dir_as_complete(tmp_path: Path):
    cache_root = tmp_path / "yt-cache"
    (cache_root / "abc12345").mkdir(parents=True)
    (cache_root / "abc12345" / "source.mp4").write_bytes(b"x" * 1024)

    (entry,) = cache_list(cache_root)
    assert entry.complete is True


def test_cache_list_survives_an_unreadable_root(monkeypatch, tmp_path: Path):
    """The root itself may be the wall — that is ``doctor``'s cue, not an exception."""
    cache_root = tmp_path / "yt-cache"
    (cache_root / "abc12345").mkdir(parents=True)
    _deny_reads(monkeypatch, cache_root)

    with pytest.raises(OSError):
        cache_list(cache_root)
