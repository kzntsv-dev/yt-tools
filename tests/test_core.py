"""Tests for pure helpers: video-id extraction, mm:ss conversions, cache path layout."""

from pathlib import Path

import pytest

from yt_tools.core import (
    cache_dir_for,
    extract_video_id,
    format_seconds_for_filename,
    format_seconds_to_mmss,
    frame_filename,
    parse_timestamp_to_seconds,
)


class TestFormatSecondsForFilename:
    def test_under_minute(self):
        assert format_seconds_for_filename(10) == "0010"

    def test_one_minute(self):
        assert format_seconds_for_filename(60) == "0100"

    def test_mid(self):
        assert format_seconds_for_filename(83) == "0123"

    def test_two_minutes(self):
        assert format_seconds_for_filename(120) == "0200"

    def test_over_hour(self):
        assert format_seconds_for_filename(3907) == "010507"

    def test_negative_clamped(self):
        assert format_seconds_for_filename(-5) == "0000"


class TestExtractVideoId:
    def test_watch_url(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_no_www(self):
        assert extract_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url_with_query(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"

    def test_watch_url_extra_params(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLfoo") == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_bare_id(self):
        assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            extract_video_id("https://example.com/not-a-youtube-url")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            extract_video_id("")


class TestFormatSecondsToMmss:
    def test_zero(self):
        assert format_seconds_to_mmss(0) == "0:00"

    def test_under_minute(self):
        assert format_seconds_to_mmss(7) == "0:07"

    def test_one_minute_exact(self):
        assert format_seconds_to_mmss(60) == "1:00"

    def test_mid_minute(self):
        assert format_seconds_to_mmss(83) == "1:23"

    def test_fractional_rounds_floor(self):
        assert format_seconds_to_mmss(83.7) == "1:23"

    def test_over_hour_uses_hmmss(self):
        # 1h 5m 7s
        assert format_seconds_to_mmss(3907) == "1:05:07"

    def test_exact_hour(self):
        assert format_seconds_to_mmss(3600) == "1:00:00"


class TestParseTimestampToSeconds:
    def test_mmss(self):
        assert parse_timestamp_to_seconds("1:23") == 83

    def test_mmss_zero_padded(self):
        assert parse_timestamp_to_seconds("01:23") == 83

    def test_hmmss(self):
        assert parse_timestamp_to_seconds("1:05:07") == 3907

    def test_bare_seconds(self):
        assert parse_timestamp_to_seconds("42") == 42

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_timestamp_to_seconds("foo")

    def test_too_many_parts_raises(self):
        with pytest.raises(ValueError):
            parse_timestamp_to_seconds("1:2:3:4")


class TestCacheDirFor:
    def test_default_cwd(self, tmp_path: Path):
        d = cache_dir_for("https://www.youtube.com/watch?v=abcDEF12345", base=tmp_path)
        assert d == tmp_path / "yt-cache" / "abcDEF12345"

    def test_creates_parent_yt_cache(self, tmp_path: Path):
        d = cache_dir_for("https://youtu.be/abcDEF12345", base=tmp_path)
        assert d.parent.name == "yt-cache"
        assert d.parent.parent == tmp_path


# --- Имена кадров (task:2782, issue:74) --------------------------------------

def test_frame_filename_is_plain_for_whole_seconds():
    assert frame_filename(90.0) == "frame_0130.jpg"
    assert frame_filename(0.0) == "frame_0000.jpg"
    assert frame_filename(3661.0) == "frame_010101.jpg"


def test_frame_filename_carries_subseconds_for_fractional_timestamps():
    # До правки 90.1 и 90.25 писались в один файл `frame_0130.jpg`, и ffmpeg -y молча
    # затирал первый кадр.
    assert frame_filename(90.1) == "frame_0130_100.jpg"
    assert frame_filename(90.25) == "frame_0130_250.jpg"
    assert frame_filename(90.999) == "frame_0130_999.jpg"


def test_frame_filename_is_a_pure_function_of_the_timestamp():
    # Ни списка таймкодов, ни состояния диска: имя зависит только от момента, иначе
    # кэш `yt-watch` («файл есть — не извлекаем») подсунул бы чужой кадр.
    assert frame_filename(12.4) == frame_filename(12.4)
    assert frame_filename(12.4) != frame_filename(12.1)
    assert frame_filename(12.4) != frame_filename(12.0)


def test_frame_filename_rounds_to_milliseconds_without_spilling_into_the_next_second():
    assert frame_filename(12.9996) == "frame_0013.jpg"
    assert frame_filename(-1.5) == "frame_0000.jpg"


def test_frame_filename_stem_matches_the_ocr_contract():
    # yt-ocr читает `^frame_(\d{4}|\d{6})(?:_\d{3})?\.jpg$`: ширина основы не должна
    # уехать (часы добавляют две цифры), иначе кадр молча выпадет из OCR.
    assert frame_filename(0.0).split("_", 1)[1].split(".")[0].isdigit()
    # до 99:59:59 (за этой границей `format_seconds_for_filename` расширяет часы —
    # предсуществующее поведение, для 100-часового ролика yt-ocr такие кадры пропустит)
    for seconds in (0.0, 59.9, 3599.0, 3600.5, 356399.0):
        stem = frame_filename(seconds).removeprefix("frame_").split(".")[0]
        assert len(stem.split("_")[0]) in (4, 6), frame_filename(seconds)
