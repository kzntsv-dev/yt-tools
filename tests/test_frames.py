"""Tests for yt-frames subprocess error propagation."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from yt_tools import frames as frames_mod
from yt_tools.core import format_seconds_for_filename, frame_filename

VIDEO_URL = "https://youtu.be/dQw4w9WgXcQ"


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["yt-dlp"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_ensure_source_mp4_missing_ffmpeg_raises_explicit_error(tmp_path: Path):
    # Reproduce reported failure: ffmpeg absent on PATH → user got
    # 'yt-dlp source download failed:' with empty body. After fix, ffmpeg must
    # be pre-checked so the error names ffmpeg, not yt-dlp.
    def which(name: str):
        return None if name == "ffmpeg" else "/fake/path/" + name

    with patch.object(frames_mod.shutil, "which", side_effect=which):
        with pytest.raises(RuntimeError, match=r"ffmpeg"):
            frames_mod._ensure_source_mp4("https://youtu.be/x", tmp_path / "src.mp4")


def test_ensure_source_mp4_yt_dlp_failure_includes_stderr(tmp_path: Path):
    # yt-dlp exits non-zero with stderr — error message must surface it.
    with patch.object(frames_mod.shutil, "which", return_value="/fake/bin"), \
         patch.object(frames_mod.subprocess, "run", return_value=_completed(1, stderr="ERROR: Private video")):
        with pytest.raises(RuntimeError, match=r"Private video"):
            frames_mod._ensure_source_mp4("https://youtu.be/x", tmp_path / "src.mp4")


def test_subprocess_failure_with_empty_stderr_includes_stdout_or_no_output_hint(tmp_path: Path):
    # When both stderr and stdout are empty, message must say so explicitly
    # instead of trailing the prefix with a bare colon and nothing.
    with patch.object(frames_mod.shutil, "which", return_value="/fake/bin"), \
         patch.object(frames_mod.subprocess, "run", return_value=_completed(1, stdout="", stderr="")):
        with pytest.raises(RuntimeError) as ei:
            frames_mod._ensure_source_mp4("https://youtu.be/x", tmp_path / "src.mp4")
    msg = str(ei.value)
    # The message must not end with a bare colon-and-nothing — that was the original bug.
    assert not msg.rstrip().endswith(":")
    assert "exit 1" in msg or "no output" in msg.lower()


def test_subprocess_failure_with_only_stdout_surfaces_stdout(tmp_path: Path):
    # yt-dlp occasionally writes diagnostic info to stdout. Falling back to stdout
    # when stderr is empty keeps the user from losing the cause.
    with patch.object(frames_mod.shutil, "which", return_value="/fake/bin"), \
         patch.object(frames_mod.subprocess, "run", return_value=_completed(1, stdout="HTTP 403 Forbidden", stderr="")):
        with pytest.raises(RuntimeError, match=r"403 Forbidden"):
            frames_mod._ensure_source_mp4("https://youtu.be/x", tmp_path / "src.mp4")


# --- Кадровый бюджет (task:2773, AC1–AC4 requirements:45) ---------------------
#
# Принцип контракта: явный запрос не трогаем, автоподбор — курируем. Сцены и
# интервал укладываются в бюджет; --timestamps не трогается ничем.


def _run_scene(tmp_path: Path, seconds: list[float], **kwargs):
    """``frames.run()`` в scene-режиме с заглушёнными детекцией, скачиванием и ffmpeg."""
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=list(seconds)), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file"):
        written = frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene", **kwargs)
    return written


def _frame_name(seconds: float) -> str:
    return frame_filename(seconds)


def test_thin_evenly_keeps_everything_when_limit_absent_or_generous():
    items = [float(i) for i in range(10)]
    assert frames_mod._thin_evenly(items, None) == items
    assert frames_mod._thin_evenly(items, 0) == items  # 0 = без потолка
    assert frames_mod._thin_evenly(items, 10) == items
    assert frames_mod._thin_evenly(items, 99) == items


def test_thin_evenly_keeps_first_and_last_without_duplicates():
    items = [float(i) for i in range(300)]
    thinned = frames_mod._thin_evenly(items, 100)
    assert len(thinned) == 100
    assert thinned[0] == items[0]
    assert thinned[-1] == items[-1]
    assert thinned == sorted(thinned)
    assert len(set(thinned)) == len(thinned)  # без слипшихся индексов


def test_thin_evenly_single_and_pair_boundaries():
    items = [float(i) for i in range(50)]
    assert frames_mod._thin_evenly(items, 1) == [items[0]]
    assert frames_mod._thin_evenly(items, 2) == [items[0], items[-1]]


def test_scene_mode_caps_at_default_max_frames(tmp_path: Path):
    # AC1: плотный монтаж (300 склеек) без флагов → не больше дефолтного потолка.
    written = _run_scene(tmp_path, [float(i) for i in range(300)])
    assert len(written) == frames_mod.DEFAULT_MAX_FRAMES


def test_scene_mode_cap_keeps_first_and_last_scene(tmp_path: Path):
    # AC2: усечение равномерное — хвост длинного ролика не отрезан.
    written = _run_scene(tmp_path, [float(i) for i in range(300)])
    assert written[0].name == _frame_name(0.0)
    assert written[-1].name == _frame_name(299.0)


def test_scene_mode_max_frames_zero_means_uncapped(tmp_path: Path):
    # AC3: явный запрос на всё.
    written = _run_scene(tmp_path, [float(i) for i in range(300)], max_frames=0)
    assert len(written) == 300


def test_scene_mode_max_frames_override(tmp_path: Path):
    written = _run_scene(tmp_path, [float(i) for i in range(300)], max_frames=5)
    assert len(written) == 5


def test_scene_mode_below_cap_is_untouched(tmp_path: Path):
    written = _run_scene(tmp_path, [float(i) for i in range(12)])
    assert len(written) == 12


def test_scene_mode_reports_thinning_on_stderr(tmp_path: Path, capsys):
    # D12: прозрачность — агент видит, что список был усечён, а не молча урезан.
    _run_scene(tmp_path, [float(i) for i in range(300)])
    err = capsys.readouterr().err
    assert "300" in err and "100" in err


def test_timestamps_mode_is_never_capped(tmp_path: Path):
    # AC4 + D1: явный запрос не трогаем — ни потолком, ни чем-либо ещё.
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file"):
        written = frames_mod.run(
            VIDEO_URL,
            out_dir=tmp_path / "frames",
            mode="timestamps",
            timestamps=[float(i) for i in range(300)],
            max_frames=5,
        )
    assert len(written) == 300


def test_cli_accepts_zero_as_uncapped():
    # Позитивный контроль: 0 — легальное значение («без потолка»), не ошибка ввода.
    with patch.object(frames_mod, "run", return_value=[]) as run:
        assert frames_mod.main([VIDEO_URL, "--mode", "scene", "--max-frames", "0"]) == 0
    assert run.call_args.kwargs["max_frames"] == 0


def test_cli_rejects_negative_max_frames():
    # Отрицательное — не «без потолка» по недоразумению: опечатка в знаке должна падать
    # ещё на разборе аргументов. `run` патчится, чтобы тест доказывал именно отказ гейта,
    # а не падение где-то позже по другой причине.
    with patch.object(frames_mod, "run", return_value=[]) as run:
        with pytest.raises(SystemExit) as ei:
            frames_mod.main([VIDEO_URL, "--mode", "scene", "--max-frames", "-1"])
    assert ei.value.code == 2
    assert not run.called


# --- Фолбэк на равномерную выборку (task:2774, D6/AC5 requirements:45) --------
#
# Статичный ролик (говорящая голова, скринкаст) даёт PySceneDetect одну сцену —
# агент получал один кадр на весь ролик и смотреть ему было нечего. Мало сцен →
# равномерный скан по длительности в пределах бюджета + предупреждение в stderr
# (агент обязан знать, что смотрит равномерную выборку, а не сцены).


def _run_scene_with_duration(tmp_path: Path, scenes: list[float], duration: float, **kwargs):
    """``frames.run()`` в scene-режиме, где детекция и длительность файла заглушены."""
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=list(scenes)), \
         patch.object(frames_mod, "_probe_source_duration", return_value=duration), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file"):
        written = frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene", **kwargs)
    return written


def test_uniform_timestamps_spread_over_duration_without_touching_the_end():
    # Последний сэмпл строго внутри ролика: seek ровно на длительность кадра не даёт
    # и уронил бы весь вызов на середине списка.
    ts = frames_mod._uniform_timestamps(600.0, 8)
    assert len(ts) == 8
    assert ts[0] == 0.0
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts)
    assert all(t < 600.0 for t in ts)


def test_uniform_timestamps_degenerate_inputs_are_empty():
    assert frames_mod._uniform_timestamps(0.0, 8) == []
    assert frames_mod._uniform_timestamps(600.0, 0) == []
    assert frames_mod._uniform_timestamps(600.0, 1) == [0.0]


def test_static_video_falls_back_to_more_than_one_frame(tmp_path: Path):
    # AC5: одна сцена на весь ролик — фолбэк обязан дать агенту что смотреть.
    written = _run_scene_with_duration(tmp_path, [0.0], 600.0)
    assert len(written) > 1
    assert written[0].name == _frame_name(0.0)


def test_static_video_fallback_warns_on_stderr(tmp_path: Path, capsys):
    # AC5 + D6: молчаливый фолбэк запрещён — агент должен понимать, что смотрит
    # равномерный скан, а не сцены.
    _run_scene_with_duration(tmp_path, [0.0], 600.0)
    err = capsys.readouterr().err.lower()
    assert "uniform" in err and "fallback" in err


def test_fallback_uses_the_whole_budget(tmp_path: Path):
    # Дефолтный бюджет — тот же счётчик, что и у капа: одна арифметика на оба приёма.
    written = _run_scene_with_duration(tmp_path, [0.0], 600.0)
    assert len(written) == frames_mod.DEFAULT_MAX_FRAMES
    assert written[-1].name == _frame_name(600.0 * (frames_mod.DEFAULT_MAX_FRAMES - 1) / frames_mod.DEFAULT_MAX_FRAMES)


def test_seven_scenes_fall_back_and_eight_do_not(tmp_path: Path, capsys):
    # Порог D6 — строго «меньше 8». Длительность берём такую, чтобы 100 равномерных
    # таймкодов не схлопывались в имена по секундам (issue:74).
    seven = _run_scene_with_duration(tmp_path, [float(i * 10) for i in range(7)], 600.0)
    assert len(seven) == frames_mod.DEFAULT_MAX_FRAMES
    assert "uniform" in capsys.readouterr().err.lower()

    eight = _run_scene_with_duration(tmp_path, [float(i * 10) for i in range(8)], 80.0)
    assert [p.name for p in eight] == [_frame_name(float(i * 10)) for i in range(8)]
    assert "scene(s) detected" not in capsys.readouterr().err.lower()


def test_fallback_extracts_the_uniform_timestamps_it_computed(tmp_path: Path):
    # Иначе баг «посчитали равномерно, а извлекаем сцены» прошёл бы мимо всех тестов.
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[0.0]), \
         patch.object(frames_mod, "_probe_source_duration", return_value=600.0), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file") as extract:
        frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene", max_frames=4)
    assert [call.args[1] for call in extract.call_args_list] == [0.0, 150.0, 300.0, 450.0]


def test_fallback_respects_explicit_budget(tmp_path: Path):
    written = _run_scene_with_duration(tmp_path, [0.0], 600.0, max_frames=5)
    assert len(written) == 5


def test_fallback_honours_a_cap_of_one(tmp_path: Path, capsys):
    # Явное число агента не переспрашиваем: попросил один кадр — получил один кадр,
    # но предупреждение о фолбэке всё равно приходит.
    written = _run_scene_with_duration(tmp_path, [0.0], 600.0, max_frames=1)
    assert [p.name for p in written] == [_frame_name(0.0)]
    assert "fallback" in capsys.readouterr().err.lower()


def test_fallback_uncapped_stays_bounded(tmp_path: Path):
    # --max-frames 0 — «без потолка» для автоподбора сцен; у равномерного скана
    # «всё» = каждый кадр ролика, поэтому берётся минимально осмысленный скан.
    written = _run_scene_with_duration(tmp_path, [0.0], 600.0, max_frames=0)
    assert len(written) == frames_mod.MIN_SCENE_FRAMES


def test_zero_scenes_also_fall_back(tmp_path: Path):
    # Детектор не нашёл вообще ничего — пустой ответ агенту ещё хуже одного кадра.
    written = _run_scene_with_duration(tmp_path, [], 600.0)
    assert len(written) == frames_mod.DEFAULT_MAX_FRAMES


def test_fallback_that_cannot_run_is_announced(tmp_path: Path, capsys):
    # Fail-open: без длительности равномерный скан не построить — отдаём что есть, не
    # падаем. Но молчать об этом нельзя: агент должен понимать, что вместо скана ему
    # достались одни сцены (а их меньше восьми).
    written = _run_scene_with_duration(tmp_path, [12.0], 0.0)
    err = capsys.readouterr().err.lower()
    assert [p.name for p in written] == [_frame_name(12.0)]
    assert "fallback" in err and ("duration" in err or "unknown" in err)


def test_enough_scenes_do_not_probe_duration_at_all(tmp_path: Path):
    # Лишнее открытие файла на нормальном ролике не нужно.
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[float(i * 10) for i in range(20)]), \
         patch.object(frames_mod, "_probe_source_duration") as probe, \
         patch.object(frames_mod, "_ffmpeg_extract_from_file"):
        frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene")
    assert not probe.called


def test_interval_mode_caps_at_default_max_frames(tmp_path: Path):
    # D1/D5: усечение общее для обоих режимов автоподбора, не только для scene.
    meta = {"duration": 600, "title": "t", "channel": "", "url": VIDEO_URL}
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "fetch_video_metadata", return_value=meta), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file"):
        written = frames_mod.run(
            VIDEO_URL, out_dir=tmp_path / "frames", mode="interval", interval=1.0
        )
    assert len(written) == frames_mod.DEFAULT_MAX_FRAMES
    assert written[0].name == _frame_name(0.0)
    assert written[-1].name == _frame_name(599.0)


# --- Дедуп near-duplicate кадров (task:2772, D7–D12/D14 requirements:45) ------
#
# Держим слайд — платим за копии одного кадра. Бюджет должен уходить на разные
# кадры, поэтому дедуп идёт ДО капа (D11): снятые копии освобождают бюджет, а кап
# распределяет освободившееся. Явный --timestamps не трогается ничем (D1), а любая
# ошибка дедупа не теряет кадры (D10).


def _write_frame(path: Path, intensity: int) -> Path:
    """Записать кадр с заданной яркостью — lossless PNG под .jpg-именем.

    Порог D8 (2.0/255) проверяется на точных значениях: JPEG-артефакты сделали бы
    граничный тест дрожащим, а `cv2.imread` определяет формат по содержимому.
    """
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", np.full((32, 32), intensity, dtype=np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


def _stub_extract_by_intensity(intensity_for):
    """Заглушка извлечения, которая пишет настоящий файл — дедуп читает файлы."""

    def _extract(source, seconds, out_path, *args, **kwargs):
        _write_frame(out_path, int(intensity_for(seconds)))

    return _extract


def _run_dedup(tmp_path: Path, seconds: list[float], intensity_for, **kwargs) -> list[Path]:
    """scene-режим с настоящими (заглушёнными) кадрами на диске → отданные кадры."""
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=list(seconds)), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _stub_extract_by_intensity(intensity_for)):
        return frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene", **kwargs)


def test_dedup_drops_a_frame_at_the_threshold_and_keeps_one_above_it(tmp_path: Path):
    # D8: порог 2.0/255, сравнение строгое: разница ровно 2.0 — ещё копия, 3.0 — уже
    # другой кадр.
    base = _write_frame(tmp_path / "a.jpg", 10)
    at_threshold = _write_frame(tmp_path / "b.jpg", 12)
    above = _write_frame(tmp_path / "c.jpg", 13)
    assert frames_mod._dedup_frames([base, at_threshold]) == [base]
    assert frames_mod._dedup_frames([base, above]) == [base, above]


def test_dedup_compares_against_the_last_kept_frame_not_the_previous_one(tmp_path: Path):
    # D8: эталон — последний ОСТАВЛЕННЫЙ кадр. Кадр, равный отброшенному соседу, но
    # отличный от эталона, обязан выжить — иначе медленная серия «шаг в 1.5» съела бы
    # весь ролик до одного кадра.
    first = _write_frame(tmp_path / "a.jpg", 10)
    second = _write_frame(tmp_path / "b.jpg", 13)
    back_to_first = _write_frame(tmp_path / "c.jpg", 10)
    assert frames_mod._dedup_frames([first, second, back_to_first]) == [first, second, back_to_first]


def test_identical_candidates_collapse_to_one_frame(tmp_path: Path):
    # AC6: N подряд идентичных кадров → 1, и лишние файлы не остаются на диске.
    written = _run_dedup(tmp_path, [float(i * 10) for i in range(20)], lambda s: 128)
    assert len(written) == 1
    assert [p.name for p in (tmp_path / "frames").glob("frame_*.jpg")] == [_frame_name(0.0)]


def test_frozen_fallback_scan_also_collapses_to_one_frame(tmp_path: Path, capsys):
    # AC6 × D6: «в том числе когда ролик сам ушёл в равномерный скан фолбэка». Иначе
    # заморожённый ролик без сцен отдавал бы агенту сотню копий одного кадра.
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[0.0]), \
         patch.object(frames_mod, "_probe_source_duration", return_value=600.0), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _stub_extract_by_intensity(lambda s: 128)):
        written = frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene")
    err = capsys.readouterr().err.lower()
    assert len(written) == 1
    assert "uniform" in err, "фолбэк обязан быть назван"
    assert "dedup dropped" in err, "и дедуп тоже — иначе агент не поймёт, почему кадр один"


def test_candidates_that_differ_are_all_kept(tmp_path: Path):
    # Обратный контроль: дедуп не «экономит» на разных кадрах.
    written = _run_dedup(tmp_path, [float(i * 10) for i in range(20)], lambda s: 10 + 3 * int(s // 10))
    assert len(written) == 20


def test_timestamps_mode_is_never_deduped(tmp_path: Path):
    # D1: явный запрос не трогаем — даже если все кадры идентичны.
    out = tmp_path / "frames"
    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _stub_extract_by_intensity(lambda s: 128)):
        written = frames_mod.run(
            VIDEO_URL, out_dir=out, mode="timestamps", timestamps=[float(i) for i in range(20)]
        )
    assert len(written) == 20


def test_no_dedup_switch_keeps_identical_candidates(tmp_path: Path):
    # D7: дедуп выключаем флагом, и тогда копии остаются.
    written = _run_dedup(tmp_path, [float(i * 10) for i in range(20)], lambda s: 128, dedup=False)
    assert len(written) == 20


def test_cli_no_dedup_flag_disables_dedup():
    with patch.object(frames_mod, "run", return_value=[]) as run:
        assert frames_mod.main([VIDEO_URL, "--mode", "scene", "--no-dedup"]) == 0
    assert run.call_args.kwargs["dedup"] is False


def test_dedup_fails_open_on_an_unreadable_frame(tmp_path: Path):
    # AC7/D10: битый кадр остаётся — терять настоящий кадр дороже, чем один раз
    # заплатить за копию. Заметь: настоящий дубль после него дедуп всё равно снимает —
    # fail-open касается нечитаемого кадра, а не всего списка.
    paths = [_write_frame(tmp_path / f"f{i}.jpg", 10) for i in range(3)]
    paths[1].write_bytes(b"not an image at all")
    kept = frames_mod._dedup_frames(paths)
    assert paths[0] in kept and paths[1] in kept


def test_dedup_fails_open_when_the_metric_breaks_for_one_frame(tmp_path: Path):
    # AC7/D10: поломка внутри метрики не теряет кадры — сломанный остаётся, а остальные
    # продолжают дедупиться (иначе достаточно было бы любого исключения, чтобы дедуп
    # тихо перестал работать целиком и никто бы этого не заметил).
    paths = [_write_frame(tmp_path / f"f{i}.jpg", 10) for i in range(3)]
    calls = {"n": 0}

    def _signature(path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("cv2 died")
        return frames_mod._frame_signature(path)

    with patch.object(frames_mod, "_frame_signature", side_effect=_signature):
        kept = frames_mod._dedup_frames(paths)
    assert paths[0] in kept and paths[1] in kept


def test_dedup_runs_before_the_cap(tmp_path: Path):
    # D11: копии снимаются первыми, поэтому бюджет достаётся разным кадрам. Половина
    # кандидатов — один и тот же кадр; после дедупа остаётся 1 + 82 разных, и кап 50
    # набирается из разных, а не из «первых 50 кандидатов, из которых 33 — копии».
    seconds = [float(i) for i in range(246)]
    written = _run_dedup(
        tmp_path,
        seconds,
        lambda s: 10 if s < 164 else 10 + 3 * int(s - 164),
        max_frames=50,
    )
    assert len(written) == 50
    assert written[0].name == _frame_name(0.0)
    assert written[-1].name == _frame_name(245.0)  # хвост диапазона не отрезан (D4)


def test_dedup_and_cap_report_their_counts_on_stderr(tmp_path: Path, capsys):
    # D12: агент видит, сколько кадров снял дедуп и сколько усекал кап.
    _run_dedup(
        tmp_path,
        [float(i) for i in range(246)],
        lambda s: 10 if s < 164 else 10 + 3 * int(s - 164),
        max_frames=50,
    )
    err = capsys.readouterr().err.lower()
    assert "dedup" in err and "164" in err
    assert "thinned" in err and "50" in err


def test_frames_the_agent_never_gets_are_not_announced_as_written(tmp_path: Path, capsys):
    # D15: stdout не объявляет кадр, которого агент не получит.
    written = _run_dedup(tmp_path, [float(i * 10) for i in range(20)], lambda s: 128)
    out = capsys.readouterr().out
    assert out.count("Wrote:") == len(written) == 1


def test_cap_deletes_the_candidates_it_thinned_away(tmp_path: Path):
    # D15 и для капа, а не только для дедупа: файлы, которых агент не получит, уходят
    # с диска (иначе их подберёт yt-ocr и вшивание кадров).
    written = _run_dedup(
        tmp_path, [float(i * 10) for i in range(20)], lambda s: 10 + 12 * int(s // 10), max_frames=5
    )
    on_disk = sorted(p.name for p in (tmp_path / "frames").glob("frame_*.jpg"))
    assert len(written) == 5
    assert on_disk == sorted(p.name for p in written)


def test_dedup_may_drop_an_identical_last_candidate(tmp_path: Path):
    # D14: кап защищает последний ВЫЖИВШИЙ кадр, а не запрещает дедупу трогать хвост.
    # Хвост 110…190s — копия кадра 100s, поэтому последний отданный кадр — 100s, а не
    # 190s: идентичный хвост не несёт информации, терять нечего.
    written = _run_dedup(
        tmp_path,
        [float(i * 10) for i in range(20)],
        lambda s: 10 + 12 * int(s // 10) if s < 100 else 250,
    )
    assert written[-1].name == _frame_name(100.0)


def test_a_single_unextractable_candidate_still_raises(tmp_path: Path):
    # Иначе автоподбор на битом ролике молча отдавал бы пустоту.
    def _extract(*args, **kwargs):
        raise RuntimeError("ffmpeg extract at 0.0s failed (exit 1)")

    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[0.0]), \
         patch.object(frames_mod, "_probe_source_duration", return_value=600.0), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _extract):
        with pytest.raises(RuntimeError, match="no frames extracted"):
            frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene")


# --- Автоподбор терпит недоступный кадр (правка 2774 по живому прогону) -------
#
# Длительность контейнера может быть на кадр-два больше, чем реально декодируемый
# материал (замерено: клип 12.0s, контейнер говорит 12.4s). Тогда последний сэмпл
# равномерного скана попадает за конец ролика, и `ffmpeg -ss` не отдаёт ничего.
# Уронить на этом весь вызов нельзя: агент потерял бы 99 годных кадров из-за одного
# хвостового. Явный --timestamps остаётся строгим — там момент назвал человек.


def test_auto_selection_skips_a_candidate_ffmpeg_cannot_extract(tmp_path: Path, capsys):
    def _extract(source, seconds, out_path, *args, **kwargs):
        if seconds > 90.0:
            raise RuntimeError(f"ffmpeg extract at {seconds}s failed (exit 1)")
        _write_frame(out_path, 20 + int(seconds))

    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[float(i * 10) for i in range(20)]), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _extract):
        written = frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene")
    assert len(written) == 10
    assert "skipped" in capsys.readouterr().err.lower()


def test_auto_selection_raises_when_no_candidate_can_be_extracted(tmp_path: Path):
    # Иначе сломанный тулчейн (нет ffmpeg, битый файл) молча отдавал бы пустоту.
    def _extract(*args, **kwargs):
        raise RuntimeError("ffmpeg not found on PATH")

    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_detect_scene_timestamps", return_value=[float(i * 10) for i in range(20)]), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _extract):
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="scene")


def test_timestamps_mode_stays_strict_about_a_bad_timestamp(tmp_path: Path):
    def _extract(*args, **kwargs):
        raise RuntimeError("ffmpeg extract at 5.0s failed (exit 1)")

    with patch.object(frames_mod, "_ensure_source_mp4", return_value=tmp_path / "source.mp4"), \
         patch.object(frames_mod, "_ffmpeg_extract_from_file", _extract):
        with pytest.raises(RuntimeError, match="5.0s"):
            frames_mod.run(VIDEO_URL, out_dir=tmp_path / "frames", mode="timestamps", timestamps=[5.0])


# --- Уникальные имена кадров (task:2782, issue:74) ---------------------------
#
# `frame_<mmss>.jpg` режет время до целых секунд, а ffmpeg вызывается с `-y`: два
# таймкода внутри секунды писались в один файл, второй молча затирал первый. Агент
# видел меньше кадров, чем ему объявили, а дедуп считал урезанный набор.


def test_candidates_inside_one_second_become_separate_files(tmp_path: Path):
    seconds = [10.1, 10.4, 10.9, 11.2, 12.3, 13.4, 14.5, 15.6]  # 8 сцен — фолбэк не нужен
    written = _run_dedup(tmp_path, seconds, lambda s: 10 + int(s) % 200, dedup=False, max_frames=0)
    names = sorted(p.name for p in written)
    assert names == [
        "frame_0010_100.jpg",
        "frame_0010_400.jpg",
        "frame_0010_900.jpg",
        "frame_0011_200.jpg",
        "frame_0012_300.jpg",
        "frame_0013_400.jpg",
        "frame_0014_500.jpg",
        "frame_0015_600.jpg",
    ]
    on_disk = sorted(p.name for p in (tmp_path / "frames").glob("frame_*.jpg"))
    assert on_disk == names


def test_uniform_scan_reports_as_many_frames_as_it_writes(tmp_path: Path):
    # AC task:2782: сколько строк `Wrote:`, столько файлов на диске. До правки 100
    # кандидатов на ролике короче 100 секунд давали ~30 уникальных имён.
    seconds = [30.0 * i / 100 for i in range(100)]
    written = _run_dedup(tmp_path, seconds, lambda s: 10 + int(s) % 200, dedup=False, max_frames=0)
    on_disk = list((tmp_path / "frames").glob("frame_*.jpg"))
    assert len(written) == 100
    assert len({p.name for p in written}) == 100
    assert len(on_disk) == 100
