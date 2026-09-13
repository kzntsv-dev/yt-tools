"""End-to-end CLI smoke tests with subprocess + youtube-transcript-api mocked.

We don't hit the real network — these tests verify the wiring (argparse parsing,
output paths, stdout contract) without touching yt-dlp / ffmpeg / YouTube.
"""

from __future__ import annotations

import contextlib
import io
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from yt_tools import cache as cache_mod
from yt_tools import cli as umbrella_cli
from yt_tools import frames as frames_mod
from yt_tools import transcript as transcript_mod
from yt_tools.markdown import Snippet


# ---- umbrella: cache list / prune --------------------------------------------


def _cp1251_console():
    """A Windows cp1251 console in strict mode — the stream that used to crash."""
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1251", errors="strict"), raw


def test_cache_list_survives_a_path_outside_cp1251(tmp_path):
    """``cache list`` prints cache *paths* — the same console class as doctor (F2)."""
    base = tmp_path / "видео 🎬"
    (base / "yt-cache" / "abc12345").mkdir(parents=True)
    console, raw = _cp1251_console()

    with contextlib.redirect_stdout(console):
        rc = umbrella_cli.main(["cache", "list", "--base", str(base)])
    console.flush()

    assert rc == 0
    assert "🎬" in raw.getvalue().decode("utf-8")


# ---- yt-transcript ----------------------------------------------------------


def test_yt_transcript_writes_file_and_prints_path(tmp_path, monkeypatch, capsys):
    out = tmp_path / "transcript.md"
    fake_snippets = ([Snippet("hello", 0.0, 2.0), Snippet("world", 60.0, 2.0)], "en")
    fake_meta = {"title": "Demo", "channel": "Chan", "duration": 100, "url": "https://youtu.be/abcDEF12345"}

    with patch.object(transcript_mod, "_fetch_snippets", return_value=fake_snippets), \
         patch.object(transcript_mod, "fetch_video_metadata", return_value=fake_meta):
        rc = transcript_mod.main(["https://youtu.be/abcDEF12345", "--out", str(out)])

    assert rc == 0
    captured = capsys.readouterr()
    last_line = captured.out.strip().splitlines()[-1]
    assert last_line == str(out.resolve())
    body = out.read_text(encoding="utf-8")
    assert "# Demo" in body
    assert "hello" in body and "world" in body
    assert "[0:00]" in body and "[1:00]" in body


def test_yt_transcript_distill_emits_hint_on_stderr(tmp_path, capsys):
    out = tmp_path / "t.md"
    fake_snippets = ([Snippet("hello", 0.0)], "en")
    fake_meta = {"title": "T", "channel": "", "duration": 5, "url": "u"}
    with patch.object(transcript_mod, "_fetch_snippets", return_value=fake_snippets), \
         patch.object(transcript_mod, "fetch_video_metadata", return_value=fake_meta):
        rc = transcript_mod.main(["https://youtu.be/abcDEF12345", "--out", str(out), "--distill"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "distill-hint" in captured.err
    assert "mcp__interns__transcript_distill" in captured.err


# ---- yt-frames --------------------------------------------------------------


def test_yt_frames_timestamps_mode_writes_each_frame(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "frames"

    def fake_ensure(url, dest, yt_dlp_bin="yt-dlp"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")
        return dest

    def fake_extract(source, seconds, out_path, ffmpeg_bin="ffmpeg"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-jpg")

    with patch.object(frames_mod, "_ensure_source_mp4", side_effect=fake_ensure), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", side_effect=fake_extract):
        rc = frames_mod.main([
            "https://youtu.be/abcDEF12345",
            "--out", str(out_dir),
            "--timestamps", "1:23,4:56",
        ])

    assert rc == 0
    out_lines = capsys.readouterr().out.strip().splitlines()
    assert len(out_lines) == 2
    assert all(line.startswith("Wrote: ") for line in out_lines)
    assert (out_dir / "frame_0123.jpg").exists()
    assert (out_dir / "frame_0456.jpg").exists()


def test_yt_frames_requires_mode(capsys):
    with pytest.raises(SystemExit):
        frames_mod.main(["https://youtu.be/abcDEF12345"])
    captured = capsys.readouterr()
    assert "--timestamps" in captured.err or "--mode" in captured.err


def test_yt_frames_interval_mode_uses_metadata_duration(tmp_path, capsys):
    out_dir = tmp_path / "frames"
    fake_meta = {"title": "T", "channel": "", "duration": 100, "url": "u"}

    def fake_ensure(url, dest, yt_dlp_bin="yt-dlp"):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-mp4")
        return dest

    def fake_extract(source, seconds, out_path, ffmpeg_bin="ffmpeg"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"x")

    with patch.object(frames_mod, "fetch_video_metadata", return_value=fake_meta), \
         patch.object(frames_mod, "_ensure_source_mp4", side_effect=fake_ensure), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", side_effect=fake_extract):
        rc = frames_mod.main([
            "https://youtu.be/abcDEF12345",
            "--out", str(out_dir),
            "--mode", "interval",
            "--interval", "30s",
        ])
    assert rc == 0
    # 0, 30, 60, 90 — four frames in a 100s video at 30s interval
    assert len(list(out_dir.glob("frame_*.jpg"))) == 4


# ---- yt-tools cache ---------------------------------------------------------


def test_yt_tools_cache_list_empty(tmp_path, capsys):
    rc = umbrella_cli.main(["cache", "list", "--base", str(tmp_path)])
    assert rc == 0
    assert "(empty:" in capsys.readouterr().out


def test_yt_tools_cache_list_reports_entries(tmp_path, capsys):
    (tmp_path / "yt-cache" / "abc12345").mkdir(parents=True)
    (tmp_path / "yt-cache" / "abc12345" / "f.bin").write_bytes(b"x" * 2048)
    rc = umbrella_cli.main(["cache", "list", "--base", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "abc12345" in out
    assert "total: 1 videos" in out


def test_yt_tools_cache_prune_noop_when_empty(tmp_path, capsys):
    rc = umbrella_cli.main(["cache", "prune", "--base", str(tmp_path), "--older-than", "1d"])
    assert rc == 0
    assert "(nothing to prune" in capsys.readouterr().out


def _deny_reads(monkeypatch, blocked: Path):
    """Deny reads of ``blocked`` the way a real permission wall does (F4: Windows
    cannot produce one with chmod, so the denial sits at the scan calls).

    Both spellings are denied on purpose: ``os.walk`` scans, and ``Path.iterdir``
    scans on 3.13+ but lists on 3.12 and older.
    """
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


def test_cache_list_on_an_unreadable_root_names_the_cause(monkeypatch, tmp_path, capsys):
    """M1 ([[task:2797]] review): the helper may raise, the CLI must explain.

    ``doctor`` was guarded in 2797; ``yt-tools cache list`` was not, so the same
    unreadable ``yt-cache`` that produced a warn report produced a traceback here.
    """
    root = tmp_path / "yt-cache"
    (root / "abc12345").mkdir(parents=True)
    _deny_reads(monkeypatch, root)

    rc = umbrella_cli.main(["cache", "list", "--base", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc != 0
    assert "Traceback" not in captured.err
    assert str(root) in captured.err
    assert "Permission denied" in captured.err  # the cause, not just "failed"


def test_cache_prune_on_an_unreadable_root_names_the_cause(monkeypatch, tmp_path, capsys):
    """M1, the other entry point: prune lists before it removes, so it dies too."""
    root = tmp_path / "yt-cache"
    (root / "abc12345").mkdir(parents=True)
    _deny_reads(monkeypatch, root)

    rc = umbrella_cli.main(["cache", "prune", "--base", str(tmp_path), "--older-than", "7d"])
    captured = capsys.readouterr()

    assert rc != 0
    assert "Traceback" not in captured.err
    assert str(root) in captured.err
    assert "Permission denied" in captured.err


def test_cache_list_does_not_pass_a_lower_bound_off_as_the_size(monkeypatch, tmp_path, capsys):
    """M3: after 2797 the size can be a lower bound — the listing must say so.

    ``doctor`` says it since 2797; the CLI printed the same partial sum as a
    confident total, which is the half of F1 it was supposed to have closed.
    """
    entry = tmp_path / "yt-cache" / "abc12345"
    blocked = entry / "sub"
    blocked.mkdir(parents=True)
    (entry / "other.mp4").write_bytes(b"y" * 512)
    _deny_reads(monkeypatch, blocked)

    rc = umbrella_cli.main(["cache", "list", "--base", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0  # a partial read is not a failure, it is a qualified number
    assert "512 B" in out
    assert "(lower bound)" in out  # the entry itself is marked
    assert "lower bound:" in out  # and so is the total built from it


def test_cache_list_prints_a_plain_total_when_everything_is_readable(tmp_path, capsys):
    """A guard, not a regression test: it cannot fail against the pre-fix code.

    What it pins is the *other* half of M3 — the qualifier has to be earned by an
    actual unreadable subtree, or "lower bound everywhere" would pass the other
    test while telling every healthy cache its size is a guess.
    """
    (tmp_path / "yt-cache" / "abc12345").mkdir(parents=True)
    (tmp_path / "yt-cache" / "abc12345" / "f.bin").write_bytes(b"x" * 2048)

    rc = umbrella_cli.main(["cache", "list", "--base", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "lower bound" not in out


def test_cache_commands_route_through_base(tmp_path, capsys):
    """``--base`` must reach ``cache.cache_root`` — the whole point of the move.

    Two caches, one old dir in each: `list` shows only the base's videos and
    `prune` removes only the base's — everything else proves the flag is ignored
    and the cwd cache is silently used instead ([[task:2799]] gap 1).
    """
    mine = tmp_path / "mine"
    other = tmp_path / "other"
    stale = mine / "yt-cache" / "oldvid123"
    stale.mkdir(parents=True)
    untouched = other / "yt-cache" / "keepme123"
    untouched.mkdir(parents=True)
    old_timestamp = time.time() - 14 * 86400
    os.utime(stale, (old_timestamp, old_timestamp))
    os.utime(untouched, (old_timestamp, old_timestamp))

    assert umbrella_cli.main(["cache", "list", "--base", str(mine)]) == 0
    out = capsys.readouterr().out
    assert "oldvid123" in out
    assert "keepme123" not in out, "--base was ignored: another cache leaked into the listing"

    assert umbrella_cli.main(["cache", "prune", "--base", str(mine), "--older-than", "7d"]) == 0
    assert "oldvid123" in capsys.readouterr().out
    assert not stale.exists()
    assert untouched.exists(), "--base was ignored: prune reached outside the base"


def test_cache_prune_rejects_a_bad_age_without_a_traceback(tmp_path, capsys):
    rc = umbrella_cli.main(["cache", "prune", "--base", str(tmp_path), "--older-than", "soon"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid duration" in captured.err
    assert "Traceback" not in captured.err
