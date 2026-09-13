"""Tests for the «no transcript» path: clear refusal + frames-only ``yt-watch`` (task:2777).

requirements:45 AC9. Живой вход: YouTube отдаёт пустое тело, библиотека кидает сырой
``xml.etree.ElementTree.ParseError``, и наружу уходило ``error: no element found: line 1,
column 0`` — из чего агент не выводит ни «нет субтитров», ни что делать вместо.
"""

from __future__ import annotations

import xml.etree.ElementTree
from pathlib import Path
from unittest.mock import patch

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

from yt_tools import transcript as transcript_mod
from yt_tools import watch as watch_mod
from yt_tools.markdown import Snippet

URL = "https://youtu.be/dQw4w9WgXcQ"
META = {"title": "T", "channel": "", "duration": 600, "url": URL}


class _FakeApi:
    """Заглушка ``YouTubeTranscriptApi``: ``fetch`` кидает заданное исключение."""

    def __init__(self, exc: Exception, fetched=None):
        self._exc = exc
        self._fetched = fetched

    def fetch(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._fetched


class _FakeTranscriptList:
    def __str__(self) -> str:
        return ""

    def __iter__(self):
        return iter(())


def _api_raising(exc: Exception):
    return patch("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **kw: _FakeApi(exc))


class _FakeFetched:
    """Ответ API без исключения: ноль сегментов, но объект валидный."""

    language_code = "en"

    def __iter__(self):
        return iter(())


def _api_returning(fetched):
    return patch("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **kw: _FakeApi(None, fetched))


def _run_transcript(tmp_path: Path, exc: Exception | None, langs: str = "en", fetched=None):
    out = tmp_path / "transcript.md"
    with _api_returning(fetched) if exc is None else _api_raising(exc), \
         patch.object(transcript_mod, "fetch_video_metadata", return_value=dict(META)):
        rc = transcript_mod.main([URL, "--out", str(out), "--lang", langs])
    return rc, out


# --- Причина отказа: типизированная, с альтернативами -------------------------


def test_fetch_snippets_wraps_a_library_failure_in_a_typed_error():
    # Шов, на который опирается yt-watch: библиотека может кидать что угодно, наружу
    # идёт один тип, и он помнит исходную причину.
    with _api_raising(TranscriptsDisabled("dQw4w9WgXcQ")):
        with pytest.raises(transcript_mod.TranscriptUnavailable) as ei:
            transcript_mod._fetch_snippets("dQw4w9WgXcQ", ["en"])
    assert isinstance(ei.value.__cause__, TranscriptsDisabled)


def test_disabled_captions_refuse_names_both_alternatives(tmp_path: Path, capsys):
    # AC1: ненулевой код + имя причины + куда идти вместо (yt-listen / yt-ocr).
    rc, out = _run_transcript(tmp_path, TranscriptsDisabled("dQw4w9WgXcQ"))

    err = capsys.readouterr().err
    assert rc == 1
    assert "yt-listen" in err and "yt-ocr" in err
    assert "disabled" in err
    assert not out.exists(), "артефакт не пишем, если текста нет"


def test_no_captions_in_requested_languages_names_them(tmp_path: Path, capsys):
    # AC1: «нет субтитров» и «нет субтитров на ru,en» — разные причины, вторая
    # подсказывает поставить другой --lang.
    exc = NoTranscriptFound("dQw4w9WgXcQ", ["ru", "en"], _FakeTranscriptList())
    rc, _ = _run_transcript(tmp_path, exc, langs="ru,en")

    err = capsys.readouterr().err
    assert rc == 1
    assert "ru" in err and "en" in err
    assert "yt-listen" in err and "yt-ocr" in err


def test_library_reason_is_shortened_not_pasted_verbatim(tmp_path: Path, capsys):
    # Причины библиотеки — простыни со ссылками на её README и инструкциями. В сообщении
    # должна быть первая осмысленная строка, а не эта простыня целиком.
    from youtube_transcript_api import VideoUnavailable

    rc, _ = _run_transcript(tmp_path, VideoUnavailable("dQw4w9WgXcQ"))

    err = capsys.readouterr().err
    assert rc == 1
    assert "no longer available" in err
    assert "github.com/jdepoix" not in err, "чужой README не тащим в свой вывод"
    assert "yt-listen" in err and "yt-ocr" in err


def test_library_version_drift_is_not_disguised_as_missing_captions():
    # TypeError/AttributeError от несовпадения версии библиотеки (или от нашего бага) —
    # это НЕ «нет субтитров»: превратив их в причину, мы получаем молчаливый баг,
    # замаскированный под совет «смотри yt-ocr».
    with _api_raising(TypeError("fetch() got an unexpected keyword argument 'languages'")):
        with pytest.raises(TypeError):
            transcript_mod._fetch_snippets("dQw4w9WgXcQ", ["en"])


def test_an_empty_transcript_is_the_same_refusal(tmp_path: Path, capsys):
    # API ответил без исключения, но нулём сегментов: текста нет. Иначе yt-transcript
    # напишет пустой артефакт, а yt-watch — кадры без объяснения, почему текста нет.
    rc, out = _run_transcript(tmp_path, None, fetched=_FakeFetched())

    err = capsys.readouterr().err
    assert rc == 1
    assert not out.exists()
    assert "empty" in err.lower()
    assert "yt-listen" in err and "yt-ocr" in err


def test_watch_degrades_on_an_empty_transcript(tmp_path: Path, capsys, frames_stack):
    # Тот же пустой ответ, но со стороны yt-watch: кадры важнее, чем отсутствие текста.
    def _extract(source, seconds, out_path, *args, **kwargs):
        _write_frame(out_path, 5 * int(seconds) + 10)

    with _api_returning(_FakeFetched()), \
         patch.object(watch_mod, "fetch_video_metadata", return_value=dict(META)), \
         patch.object(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4"), \
         patch.object(watch_mod, "_ffmpeg_extract_from_file", _extract), \
         patch.object(watch_mod, "_scene_timestamps_with_fallback", lambda source, threshold, max_frames: [0.0, 5.0]):
        rc = watch_mod.main([URL, "--out", str(tmp_path)])

    md = (tmp_path / "watch.md").read_text(encoding="utf-8")
    assert rc == 0
    assert md.count("![scene at") == 2
    assert "Transcript unavailable" in md
    assert "warning" in capsys.readouterr().err.lower()


def test_unparsable_api_response_gives_a_named_reason(tmp_path: Path, capsys):
    # AC3: живой случай — YouTube отдал пустое тело, библиотека упала в XML-парсер.
    # Сырой ParseError не должен быть всем сообщением.
    rc, _ = _run_transcript(tmp_path, xml.etree.ElementTree.ParseError("no element found: line 1, column 0"))

    err = capsys.readouterr().err
    assert rc == 1
    assert "no element found" in err, "причина названа, а не спрятана"
    assert "ParseError" in err, "класс ошибки — часть диагноза"
    assert "yt-listen" in err and "yt-ocr" in err


# --- yt-watch: кадры есть, текста нет — не падаем -----------------------------


def _write_frame(path: Path, intensity: int) -> Path:
    import cv2
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", np.full((32, 32), intensity, dtype=np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    return path


def test_watch_degrades_to_frames_only_when_transcript_is_unavailable(tmp_path: Path, capsys, frames_stack):
    # AC2: артефакт yt-watch — кадры + (когда есть) текст. Нет текста — не повод
    # потерять кадры, которые уже извлечены.
    def _extract(source, seconds, out_path, *args, **kwargs):
        _write_frame(out_path, 5 * int(seconds) + 10)

    with _api_raising(TranscriptsDisabled("dQw4w9WgXcQ")), \
         patch.object(watch_mod, "fetch_video_metadata", return_value=dict(META)), \
         patch.object(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4"), \
         patch.object(watch_mod, "_ffmpeg_extract_from_file", _extract), \
         patch.object(watch_mod, "_scene_timestamps_with_fallback", lambda source, threshold, max_frames: [0.0, 5.0, 10.0]):
        rc = watch_mod.main([URL, "--out", str(tmp_path)])

    md = (tmp_path / "watch.md").read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert rc == 0
    assert md.count("![scene at") == 3
    assert "captions are disabled" in md, "пояснение живёт в артефакте, а не только в stderr"
    assert "yt-listen" in md and "yt-ocr" in md
    assert "warning" in err.lower() and "transcript" in err.lower()


def test_watch_with_captions_carries_no_transcript_note(tmp_path: Path, frames_stack):
    # Обратная сторона: когда текст есть, в шапке ничего лишнего.
    def _extract(source, seconds, out_path, *args, **kwargs):
        _write_frame(out_path, 5 * int(seconds) + 10)

    with patch.object(watch_mod, "fetch_video_metadata", return_value=dict(META)), \
         patch.object(watch_mod, "_fetch_snippets", lambda vid, langs: ([Snippet("body", 0.0, 2.0)], "en")), \
         patch.object(watch_mod, "_ensure_source_mp4", lambda url, dest: tmp_path / "source.mp4"), \
         patch.object(watch_mod, "_ffmpeg_extract_from_file", _extract), \
         patch.object(watch_mod, "_scene_timestamps_with_fallback", lambda source, threshold, max_frames: [0.0, 5.0]):
        watch_mod.run(URL, out_dir=tmp_path)

    md = (tmp_path / "watch.md").read_text(encoding="utf-8")
    assert "body" in md
    assert "yt-listen" not in md


def test_watch_still_fails_on_an_unexpected_transcript_bug(tmp_path: Path, capsys, frames_stack):
    # AC4: глотаем только объявленный отказ. Настоящий баг обязан быть виден.
    with patch.object(watch_mod, "fetch_video_metadata", return_value=dict(META)), \
         patch.object(watch_mod, "_fetch_snippets", side_effect=RuntimeError("boom")):
        rc = watch_mod.main([URL, "--out", str(tmp_path)])

    assert rc == 1
    assert "boom" in capsys.readouterr().err
