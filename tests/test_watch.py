"""Tests for the yt-watch interleaving renderer and its frame budget."""

import re
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from yt_tools import frames as frames_mod
from yt_tools import watch as watch_mod
from yt_tools.frames import DEFAULT_MAX_FRAMES
from yt_tools.markdown import Snippet
from yt_tools.watch import _render_interleaved

VIDEO_URL = "https://youtu.be/dQw4w9WgXcQ"

def _s(text: str, start: float, duration: float = 2.0) -> Snippet:
    return Snippet(text=text, start=start, duration=duration)


META = dict(title="T", channel="C", duration=600, lang="en", url="https://youtu.be/x")


def test_frame_inside_paragraph_appears_after():
    md = _render_interleaved(
        snippets=[_s("para one body", 0.0), _s("para two body", 60.0)],
        frame_timestamps=[10.0],
        **META,
    )
    p1 = md.index("para one body")
    img = md.index("frame_0010")
    p2 = md.index("para two body")
    assert p1 < img < p2


def test_multiple_frames_in_one_paragraph():
    md = _render_interleaved(
        snippets=[_s("only paragraph here", 0.0)],
        frame_timestamps=[5.0, 10.0, 20.0],
        **META,
    )
    assert md.count("![scene at") == 3
    body = md.index("only paragraph here")
    assert all(md.index(f"frame_{ts}") > body for ts in ("0005", "0010", "0020"))


def test_leading_frame_before_first_paragraph_emits_first():
    md = _render_interleaved(
        snippets=[_s("first", 30.0)],
        frame_timestamps=[5.0],
        **META,
    )
    img = md.index("frame_0005")
    body = md.index("first")
    assert img < body


def test_trailing_frames_after_last_paragraph():
    md = _render_interleaved(
        snippets=[_s("first", 0.0)],
        frame_timestamps=[120.0],
        **META,
    )
    body = md.index("first")
    img = md.index("frame_0200")
    assert body < img


def test_no_frames_produces_plain_paragraphs():
    md = _render_interleaved(
        snippets=[_s("solo", 0.0)],
        frame_timestamps=[],
        **META,
    )
    assert "![scene" not in md
    assert "solo" in md


def test_header_metadata_present():
    md = _render_interleaved(
        snippets=[_s("body", 0.0)],
        frame_timestamps=[],
        **META,
    )
    assert "# T" in md
    assert "Channel:" in md
    assert "Duration:" in md
    assert "Lang:" in md


def test_two_frames_in_one_second_get_two_links():
    # issue:74: markdown ссылается ровно на те имена, что пишет извлекатель, и два
    # кадра одной секунды больше не указывают на один перезаписанный файл.
    md = _render_interleaved(
        snippets=[_s("body", 0.0)],
        frame_timestamps=[10.1, 10.9],
        **META,
    )
    assert "](frames/frame_0010_100.jpg)" in md
    assert "](frames/frame_0010_900.jpg)" in md
    assert md.count("![scene at") == 2


def test_watch_never_reuses_a_stale_frame_for_another_moment(tmp_path, monkeypatch, frames_stack):
    # Регрессия на блокер, найденный ревью: если имя кадра зависит от списка сцен
    # (счётчик `_N`), то прогон с другим набором сцен попадает в свой `exists()`-кэш и
    # вшивает в markdown чужой кадр. Имя — чистая функция момента, поэтому кэш честен:
    # другой таймкод не может достаться из файла прежнего.
    from yt_tools.core import frame_filename

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / frame_filename(12.0)).write_bytes(b"stale frame of second 12.0")

    extracted: list[float] = []

    def _fake_extract(source, seconds, out_path, *args, **kwargs):
        extracted.append(seconds)
        out_path.write_bytes(b"frame")

    monkeypatch.setattr(watch_mod, "fetch_video_metadata", lambda url: {"title": "T", "channel": "", "duration": 600, "url": url})
    monkeypatch.setattr(watch_mod, "_fetch_snippets", lambda vid, langs: ([_s("body", 0.0)], "en"))
    monkeypatch.setattr(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4")
    monkeypatch.setattr(watch_mod, "_ffmpeg_extract_from_file", _fake_extract)
    monkeypatch.setattr(watch_mod, "_scene_timestamps_with_fallback", lambda source, threshold, max_frames: [12.4])

    md_path = watch_mod.run("https://youtu.be/dQw4w9WgXcQ", out_dir=tmp_path)
    assert extracted == [12.4]
    assert "](frames/frame_0012_400.jpg)" in md_path.read_text(encoding="utf-8")

    # Повторный прогон того же момента кадр не переизвлекает (кэш работает).
    watch_mod.run("https://youtu.be/dQw4w9WgXcQ", out_dir=tmp_path)
    assert extracted == [12.4]


# --- Кадровый бюджет (task:2776, requirements:45 AC8 / D4 / D6 / D7 / D11 / D15) --
#
# `yt-watch` вшивает кадры в markdown — тот же автоподбор, что и у `yt-frames`, значит
# тот же бюджет: фолбэк равномерного скана, дедуп до капа, усечение с сохранением
# границ. В `watch.md` попадают только выжившие кадры, снятые с диска удаляются, а
# stdout остаётся одной строкой — путь к `watch.md`.

_META = dict(title="T", channel="", duration=600, lang="en", url=VIDEO_URL)
_LINK_RE = re.compile(r"!\[scene at [^\]]*\]\((frames/[^)]+)\)")


def _write_frame(path: Path, intensity: int) -> Path:
    """Кадр заданной яркости — lossless PNG под .jpg-именем: дедуп читает содержимое."""
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", np.full((32, 32), intensity, dtype=np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


def _stub_extract(intensity_for):
    def _extract(source, seconds, out_path, *args, **kwargs):
        _write_frame(out_path, int(intensity_for(seconds)) % 255)

    return _extract


def _patched_watch(tmp_path: Path, scenes, intensity_for=None, extract=None) -> ExitStack:
    """Пачи прогона: метаданные / сниппеты / источник / извлечение / выбранные моменты.

    Соседние кадры отличаются на 5/255 — выше порога дедупа (2.0), поэтому усечение
    проверяется на разных кадрах, а не сворачивается дедупом. Свой извлекатель
    передаётся через ``extract``.
    """
    intensity_for = intensity_for or (lambda s: 5 * int(s) + 10)
    stack = ExitStack()
    stack.enter_context(patch.object(watch_mod, "fetch_video_metadata", lambda url: dict(_META, url=url)))
    stack.enter_context(patch.object(watch_mod, "_fetch_snippets", lambda vid, langs: ([_s("body", 0.0)], "en")))
    stack.enter_context(patch.object(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4"))
    stack.enter_context(patch.object(watch_mod, "_ffmpeg_extract_from_file", extract or _stub_extract(intensity_for)))
    stack.enter_context(
        patch.object(
            watch_mod,
            "_scene_timestamps_with_fallback",
            lambda source, threshold, max_frames: list(scenes),
        )
    )
    return stack


def _run_watch(tmp_path: Path, scenes, intensity_for=None, extract=None, **kwargs) -> str:
    with _patched_watch(tmp_path, scenes, intensity_for, extract):
        path = watch_mod.run(VIDEO_URL, out_dir=tmp_path, **kwargs)
    return path.read_text(encoding="utf-8")


def test_watch_caps_embedded_frames_at_the_default_budget(tmp_path: Path, frames_stack):
    # AC8: клип с сотнями склеек не может отдать агенту сотни кадров — тот же дефолт,
    # что и у yt-frames (D2).
    md = _run_watch(tmp_path, [float(i * 5) for i in range(300)])
    assert md.count("![scene at") == DEFAULT_MAX_FRAMES


def test_watch_static_video_falls_back_to_a_uniform_scan(tmp_path: Path, capsys, frames_stack):
    # AC8 + D6: одна сцена на весь ролик — иначе в markdown попадёт ОДИН кадр на всё
    # видео. Фолбэк идёт через ту же функцию, что у yt-frames, и обязан быть слышен.
    with ExitStack() as stack:
        stack.enter_context(patch.object(frames_mod, "_detect_scene_timestamps", return_value=[0.0]))
        stack.enter_context(patch.object(frames_mod, "_probe_source_duration", return_value=600.0))
        stack.enter_context(patch.object(watch_mod, "fetch_video_metadata", lambda url: dict(_META, url=url)))
        stack.enter_context(patch.object(watch_mod, "_fetch_snippets", lambda vid, langs: ([_s("body", 0.0)], "en")))
        stack.enter_context(patch.object(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4"))
        stack.enter_context(patch.object(watch_mod, "_ffmpeg_extract_from_file", _stub_extract(lambda s: 5 * int(s) + 10)))
        md = watch_mod.run(VIDEO_URL, out_dir=tmp_path).read_text(encoding="utf-8")
    assert md.count("![scene at") > 1
    assert "uniform" in capsys.readouterr().err.lower()


def test_watch_honours_an_explicit_max_frames(tmp_path: Path, frames_stack):
    # D4: бюджет распределяется по всему ролику — первый и последний кадр на месте,
    # хвост не обрезан.
    scenes = [float(i * 5) for i in range(20)]
    md = _run_watch(tmp_path, scenes, max_frames=3)
    assert md.count("![scene at") == 3
    assert "](frames/frame_0000.jpg)" in md
    assert "](frames/frame_0135.jpg)" in md


def test_watch_dedups_identical_frames_and_removes_the_copies(tmp_path: Path, frames_stack):
    # AC6 + D7 + D15: N идентичных кадров → одна ссылка в markdown, лишних файлов на
    # диске не остаётся (markdown не ссылается на то, чего агент не получит).
    md = _run_watch(tmp_path, [float(i * 5) for i in range(10)], intensity_for=lambda s: 128)
    assert md.count("![scene at") == 1
    assert [p.name for p in (tmp_path / "frames").glob("frame_*.jpg")] == ["frame_0000.jpg"]


def test_watch_no_dedup_keeps_the_copies(tmp_path: Path, frames_stack):
    # D7: дедуп выключаем явно — копии остаются.
    md = _run_watch(tmp_path, [float(i * 5) for i in range(10)], intensity_for=lambda s: 128, dedup=False)
    assert md.count("![scene at") == 10
    assert len(list((tmp_path / "frames").glob("frame_*.jpg"))) == 10


def test_watch_links_match_the_files_on_disk(tmp_path: Path, frames_stack):
    # AC5: каждая ссылка markdown — существующий файл, и лишних файлов нет.
    md = _run_watch(
        tmp_path,
        [float(i * 5) for i in range(10)],
        intensity_for=lambda s: 128 if s < 25 else 200 + int(s),
    )
    linked = {Path(name).name for name in _LINK_RE.findall(md)}
    on_disk = {p.name for p in (tmp_path / "frames").glob("frame_*.jpg")}
    assert linked == on_disk
    assert len(linked) > 1


def test_watch_cli_max_frames_zero_disables_the_cap(tmp_path: Path, frames_stack):
    # D3: `--max-frames 0` — явный запрос на всё (для автоподбора сцен).
    with _patched_watch(tmp_path, [float(i * 5) for i in range(20)]):
        rc = watch_mod.main([VIDEO_URL, "--out", str(tmp_path), "--max-frames", "0"])
    assert rc == 0
    md = (tmp_path / "watch.md").read_text(encoding="utf-8")
    assert md.count("![scene at") == 20


def test_watch_stdout_stays_a_single_line(tmp_path: Path, capsys, frames_stack):
    # stdout-контракт yt-watch: одна строка — путь к watch.md. `Wrote:` и `note:`
    # принадлежат yt-frames (там артефакт — сами кадры) и в stdout не протекают.
    with _patched_watch(tmp_path, [float(i * 5) for i in range(10)]):
        rc = watch_mod.main([VIDEO_URL, "--out", str(tmp_path)])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert out == [str((tmp_path / "watch.md").resolve())]


def test_watch_reports_dedup_and_cap_counts_on_stderr(tmp_path: Path, capsys, frames_stack):
    # D12: счётчики — в stderr. Агент должен понимать, почему кадров меньше, чем сцен;
    # в stdout они не идут (там контракт одной строки).
    _run_watch(
        tmp_path,
        [float(i * 5) for i in range(20)],
        intensity_for=lambda s: 128 if s < 25 else 200 + int(s),
        max_frames=4,
    )
    err = capsys.readouterr().err.lower()
    assert "dedup dropped" in err
    assert "thinned to" in err


def test_watch_skips_a_candidate_ffmpeg_cannot_extract(tmp_path: Path, capsys, frames_stack):
    # Fail-open как у автоподбора yt-frames: контейнер может обещать больше секунд, чем
    # декодирует, и терять из-за одного такого кандидата весь watch.md незачем.
    def _extract(source, seconds, out_path, *args, **kwargs):
        if seconds == 10.0:
            raise RuntimeError("ffmpeg extract at 10.0s failed (exit 1)")
        _write_frame(out_path, 5 * int(seconds) + 10)

    md = _run_watch(tmp_path, [0.0, 5.0, 10.0, 15.0], extract=_extract)
    assert md.count("![scene at") == 3
    assert "frame_0010.jpg" not in md
    assert "skipped" in capsys.readouterr().err.lower()


def test_watch_raises_when_no_candidate_can_be_extracted(tmp_path: Path, frames_stack):
    # Обратная сторона fail-open: пустой результат — это не «видео без кадров», а
    # сломанный инструмент (тот же принцип, что в `_extract_candidates`).
    def _extract(source, seconds, out_path, *args, **kwargs):
        raise RuntimeError("ffmpeg extract failed (exit 1)")

    with pytest.raises(RuntimeError, match="no frames extracted"):
        _run_watch(tmp_path, [0.0, 5.0], extract=_extract)


def test_watch_cli_reports_a_broken_ffmpeg_cleanly(tmp_path: Path, capsys, frames_stack):
    # «Инструмент сломан» — одна строка `error:` и код 1, а не трейсбек: у yt-frames
    # тот же контракт (ошибки автоподбора выходят как `error:` без стека).
    def _extract(source, seconds, out_path, *args, **kwargs):
        raise RuntimeError("ffmpeg extract failed (exit 1)")

    with _patched_watch(tmp_path, [0.0, 5.0], extract=_extract):
        rc = watch_mod.main([VIDEO_URL, "--out", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: no frames extracted" in err
    assert "Traceback" not in err
