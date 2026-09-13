#!/usr/bin/env python3
"""Pre-flight a PyPI project name *before* release day.

A 404 on ``https://pypi.org/pypi/<name>/json`` means only "no project with that
name". Warehouse additionally refuses a new project whose name is **too similar**
to an existing one: ``check_project_name`` compares ``ultranormalize_name()``
(``warehouse/packaging/services.py``) and raises ``ProjectNameUnavailableSimilar``
→ HTTP 400 "The name '…' is too similar to an existing project". The same check
guards a *pending Trusted Publisher*, so it can also block the OIDC release path.

The folding rule, verbatim from the warehouse migration that introduced it::

    lower(
      regexp_replace(                     -- drop separators
        regexp_replace(                   -- fold l L i I -> 1
          regexp_replace($1, '(\\.|_|-)', '', 'ig'),
          '(l|L|i|I)', '1', 'ig'),
        '(o|O)', '0', 'ig'))

Why this script exists: ``yt-tools`` folds to ``ytt001s`` — the same class as the
existing YouTube toolkit ``yttools`` — so the obvious name is unregisterable and
was only discovered by reading warehouse's source. That is exactly the kind of
fact a release day should not be spent on.

Usage::

    python scripts/check-pypi-name.py                    # checks the reserved name
    python scripts/check-pypi-name.py some-name          # checks another candidate
    python scripts/check-pypi-name.py --explain yt-tools # just the folding
    python scripts/check-pypi-name.py n --names-file f   # offline (tests, CI)

Exit codes: 0 available · 1 refused (taken, or too similar) · 2 the check could
not run at all (bad path, no network) — an unrunnable check is never a pass.

Caveat, found by using it (2026-09-13, the day `yt-tools-cli` was registered):
the names come from the **simple index**, which is served through a CDN cache, so
a project created minutes ago can still read as "available" here even though the
JSON API (`/pypi/<name>/json`) already returns it. The verdict is a pre-flight —
it tells you not to bother with an obvious name, not what happened in the last
hour. The registration itself is the only authoritative answer.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: The name this project will register. ``yt-tools`` itself is impossible: it
#: folds into the same class as the existing ``yttools`` (see module docstring).
RESERVED_NAME = "yt-tools-cli"

SIMPLE_INDEX = "https://pypi.org/simple/"

_ANCHOR_RE = re.compile(r"<a[^>]*>([^<]+)</a>", re.IGNORECASE)


def ultranormalize(name: str) -> str:
    """Warehouse's lossy folding — the form similarity is decided on.

    Order matters only for readability; the three substitutions are independent.
    """
    folded = re.sub(r"[._-]", "", name)
    folded = re.sub(r"[li]", "1", folded, flags=re.IGNORECASE)
    return re.sub(r"o", "0", folded, flags=re.IGNORECASE).lower()


def parse_names(text: str) -> list[str]:
    """Project names out of the simple index HTML, or out of a plain list.

    Both spellings are accepted so the offline fixture and the live index go
    through the same parser: the live one serves ``<a href=…>name</a>`` lines.
    """
    names = _ANCHOR_RE.findall(text) if "<a" in text.lower() else text.splitlines()
    return [n.strip() for n in names if n.strip()]


def fetch_names(index: str) -> list[str]:
    request = urllib.request.Request(
        index, headers={"Accept-Encoding": "gzip", "User-Agent": "yt-tools-name-check"}
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
    return parse_names(payload.decode("utf-8", "replace"))


def check(name: str, names: list[str]) -> tuple[int, list[str]]:
    """``(exit code, report lines)`` for one candidate against one name list."""
    name = name.strip().lower()
    normalized = {n.lower() for n in names}
    folded = ultranormalize(name)
    lines = [
        f"pypi name check: {name}",
        f"  ultranormalized form   : {folded}",
        f"  names compared against : {len(normalized)}",
    ]

    if name in normalized:
        lines.append(f"  verdict                : TAKEN - the project {name!r} already exists")
        lines.append(
            "  note                   : taken is not the same refusal as too-similar:"
            " a PEP 541 claim is a different conversation from a rename"
        )
        return 1, lines

    collisions = sorted({n for n in normalized if n != name and ultranormalize(n) == folded})
    if collisions:
        lines.append(f"  similar-name collisions: {', '.join(collisions)}")
        lines.append(
            f"  verdict                : REFUSED - folds into {folded}, which"
            " PyPI already has; registration fails with"
            " \"The name '...' is too similar to an existing project\""
        )
        return 1, lines

    lines.append("  similar-name collisions: none")
    lines.append("  verdict                : available - register it before someone else does")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-pypi-name",
        description="Pre-flight a PyPI project name against the name-similarity rule.",
    )
    parser.add_argument("name", nargs="?", default=RESERVED_NAME, help=f"default: {RESERVED_NAME}")
    parser.add_argument("--explain", metavar="NAME", help="print only the ultranormalized form")
    parser.add_argument("--names-file", type=Path, help="read names from a file instead of the index")
    parser.add_argument("--index", default=SIMPLE_INDEX, help=f"simple index URL (default: {SIMPLE_INDEX})")
    args = parser.parse_args(argv)

    if args.explain is not None:
        print(ultranormalize(args.explain))
        return 0

    if args.names_file is not None:
        try:
            names = parse_names(args.names_file.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"error: cannot read {args.names_file}: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            names = fetch_names(args.index)
        except (urllib.error.URLError, OSError) as exc:
            print(f"error: cannot read {args.index}: {exc}", file=sys.stderr)
            return 2

    if not names:
        print(f"error: {args.index if args.names_file is None else args.names_file} yielded no names -"
              " an empty list would report every name as available", file=sys.stderr)
        return 2

    code, lines = check(args.name, names)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
