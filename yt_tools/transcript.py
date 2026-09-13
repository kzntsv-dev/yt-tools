"""yt-transcript CLI — fetch YouTube auto-subs and emit clean markdown."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yt_tools._metadata import MetadataError, fetch_video_metadata
from yt_tools.core import cache_dir_for, extract_video_id, force_utf8_streams
from yt_tools.markdown import Snippet, snippets_to_markdown

# Единственное место, где названы альтернативы: оба CLI (отказ yt-transcript и
# деградация yt-watch) говорят одно и то же, и агент получает один и тот же выход.
TRANSCRIPT_ALTERNATIVES = (
    "try yt-listen (audio: music, speech without captions) or yt-ocr (text burned into the frames)"
)


class TranscriptUnavailable(RuntimeError):
    """Текста нет — и это названная причина, а не сырое исключение библиотеки.

    Ролик без субтитров и пустой/битый ответ API — разные диагнозы, но один выход для
    агента: кадры и звук. Оба идут этим типом, чтобы ``yt-watch`` мог отличить
    «текста нет» (деградируем, кадры ценны) от бага в коде (падаем честно).
    """


def _first_line(text: str | None) -> str | None:
    """Первая осмысленная строка причины, или ``None``.

    Причины библиотеки бывают портянками с ссылками на её README, а незаполненный
    шаблон (``{reason}``) вообще бесполезен — нам нужен короткий диагноз.
    """
    if not isinstance(text, str):
        return None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "{" in line:
            continue
        return line if len(line) <= 200 else line[:197] + "..."
    return None


def _transcript_failure_reason(exc: Exception, languages: list[str]) -> str:
    """Человеческая причина отказа: что случилось, а не какой класс упал."""
    from youtube_transcript_api import CouldNotRetrieveTranscript, NoTranscriptFound, TranscriptsDisabled

    if isinstance(exc, TranscriptsDisabled):
        return "captions are disabled for this video"
    if isinstance(exc, NoTranscriptFound):
        return f"no captions in the requested language(s): {', '.join(languages)}"
    if isinstance(exc, CouldNotRetrieveTranscript):
        # ``cause`` — свойство подклассов, и обращение к нему не должно подменить
        # исходную ошибку собственной: сообщение важнее точности формулировки.
        try:
            cause = _first_line(exc.cause)
        except Exception:
            cause = None
        return f"the transcript API could not retrieve captions ({cause or type(exc).__name__})"
    # Сюда попадает живой случай: YouTube отдаёт пустое тело, библиотека падает в XML-парсер.
    return f"the transcript API returned nothing usable ({type(exc).__name__}: {exc})"


def _fetch_snippets(video_id: str, languages: list[str]) -> tuple[list[Snippet], str]:
    """Fetch transcript snippets via youtube-transcript-api. Returns (snippets, lang).

    Любая ошибка библиотеки становится ``TranscriptUnavailable`` с названной причиной
    и альтернативами: наружу не уходит ни многословный текст библиотеки, ни сырой
    ``ParseError``. Не оборачиваются: ``ImportError`` (нет extras — это не «нет
    субтитров») и программные ошибки (``TypeError``/``AttributeError``/``NameError`` —
    дрейф версии библиотеки или наш баг, им место в трейсбеке, а не в совете «yt-ocr»).
    Пустой ответ (0 сегментов) — тот же отказ, что и отсутствие субтитров.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
    except (TypeError, AttributeError, NameError):
        # Программная ошибка (дрейф версии библиотеки, наш баг) — не «нет субтитров».
        # Обернув её, мы получим тихий баг под маской совета «смотри yt-ocr».
        raise
    except Exception as e:
        raise TranscriptUnavailable(f"{_transcript_failure_reason(e, languages)} — {TRANSCRIPT_ALTERNATIVES}") from e

    snippets = [Snippet(text=s.text, start=s.start, duration=s.duration) for s in fetched]
    if not snippets:
        # API ответил без исключения, но текста нет. Это тот же отказ, иначе
        # yt-transcript напишет пустой артефакт, а yt-watch — кадры без объяснения.
        raise TranscriptUnavailable(f"the transcript is empty (0 segments) — {TRANSCRIPT_ALTERNATIVES}")
    lang = getattr(fetched, "language_code", languages[0] if languages else "")
    return snippets, lang


def run(
    url: str,
    out: Path | None = None,
    distill: bool = False,
    languages: list[str] | None = None,
) -> Path:
    """Fetch transcript and write to a markdown file. Returns the artifact path."""
    languages = languages or ["en"]
    video_id = extract_video_id(url)

    if out is None:
        out = cache_dir_for(url) / "transcript.md"

    try:
        meta = fetch_video_metadata(url)
    except MetadataError as e:
        print(f"warning: {e} — using minimal metadata", file=sys.stderr)
        meta = {"title": video_id, "channel": "", "duration": 0, "url": url}

    snippets, lang = _fetch_snippets(video_id, languages)
    meta["lang"] = lang

    md = snippets_to_markdown(snippets, meta)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    if distill:
        print(
            f"distill-hint: invoke mcp__interns__transcript_distill on {out.resolve()}",
            file=sys.stderr,
        )

    return out.resolve()


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="yt-transcript",
        description="Fetch YouTube transcript as clean markdown with [mm:ss] anchors.",
    )
    parser.add_argument("url", help="YouTube URL or bare video id")
    parser.add_argument("--out", type=Path, default=None, help="Output path (default: ./yt-cache/<vid>/transcript.md)")
    parser.add_argument(
        "--lang",
        default="en",
        help="Comma-separated language preference (default: en). Example: ru,en",
    )
    parser.add_argument(
        "--distill",
        action="store_true",
        help="Print a hint to invoke mcp__interns__transcript_distill on the artifact.",
    )
    args = parser.parse_args(argv)

    languages = [lang.strip() for lang in args.lang.split(",") if lang.strip()]
    try:
        path = run(args.url, out=args.out, distill=args.distill, languages=languages)
    except Exception as e:
        # ``TranscriptUnavailable`` приходит сюда же: у него уже есть причина и
        # названные альтернативы, так что отдельная ветка ничего бы не добавила.
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
