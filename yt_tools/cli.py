"""``yt-tools`` umbrella CLI — ``cache list`` / ``cache prune`` / ``doctor``."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from yt_tools import doctor
from yt_tools.cache import cache_list, cache_prune, cache_root, format_size
from yt_tools.core import force_utf8_streams

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(d|day|days|h|hour|hours)?$", re.IGNORECASE)


def parse_age(spec: str) -> float:
    """Parse ``7d`` / ``12h`` / bare days. Returns days as float."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise ValueError(f"invalid duration: {spec!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "d").lower()
    if unit in ("d", "day", "days"):
        return value
    if unit in ("h", "hour", "hours"):
        return value / 24.0
    raise ValueError(f"invalid duration unit: {unit!r}")


def _cache_read_failure(root: Path, exc: OSError, *, action: str) -> int:
    """A cache that cannot be listed is a named failure, not a traceback (M1).

    ``cache_list`` stays strict — it raises, and ``doctor.check_cache`` relies on
    that raise to tell "the root is unreadable" from "a subtree is" ([[task:2797]]
    F1). Explaining the refusal is the caller's job, so the two CLI entry points
    name the cause the way the report does instead of dumping a stack.

    ``cache_prune`` also lands here, and that is exact rather than loose: it only
    ever raises out of its listing walk (removal is ``rmtree(ignore_errors=True)``),
    so the one reachable cause is the one named.
    """
    reason = exc.strerror or exc.__class__.__name__
    print(f"error: {root} cannot be read ({reason}) - nothing {action}", file=sys.stderr)
    return 1


def cmd_cache_list(args: argparse.Namespace) -> int:
    root = cache_root(args.base)
    try:
        entries = cache_list(root)
    except OSError as exc:
        return _cache_read_failure(root, exc, action="listed")
    if not entries:
        print(f"(empty: {root})")
        return 0
    now = time.time()
    total = 0
    partial = 0
    for e in entries:
        age_days = (now - e.mtime) / 86400
        # M3: after 2797 a size can be a lower bound. Printing it as an exact
        # number is the same lie doctor stopped telling — mark the entry and the
        # total built from it.
        marker = "" if e.complete else "  (lower bound)"
        print(f"{e.video_id}  {format_size(e.size_bytes):>10}  {age_days:5.1f}d  {e.path}{marker}")
        total += e.size_bytes
        partial += 0 if e.complete else 1
    # M3: after 2797 a size can be a lower bound. Printing it as an exact number is
    # the same lie doctor stopped telling — qualify the entry *and* the total built
    # from it, so a reader who stops at either line still sees the caveat.
    total_line = f"total: {len(entries)} videos, {format_size(total)}"
    if partial:
        total_line += f" (lower bound: {partial} of {len(entries)} dirs unreadable)"
    print(f"\n{total_line}")
    return 0


def cmd_cache_prune(args: argparse.Namespace) -> int:
    root = cache_root(args.base)
    try:
        days = parse_age(args.older_than)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        removed = cache_prune(root, older_than_days=days)
    except OSError as exc:
        return _cache_read_failure(root, exc, action="pruned")
    if not removed:
        print(f"(nothing to prune in {root})")
        return 0
    for p in removed:
        print(f"removed: {p}")
    print(f"\npruned {len(removed)} dirs older than {days}d")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.json:
        argv.append("--json")
    if args.base is not None:
        argv += ["--base", str(args.base)]
    return doctor.main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yt-tools", description="yt-tools umbrella CLI.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cache = sub.add_parser("cache", help="Manage source-mp4 cache")
    cache_sub = cache.add_subparsers(dest="cache_cmd", required=True)

    cache_list_p = cache_sub.add_parser("list", help="List cached videos")
    cache_list_p.add_argument("--base", type=Path, default=None, help="Base dir containing yt-cache/ (default: cwd)")
    cache_list_p.set_defaults(func=cmd_cache_list)

    cache_prune_p = cache_sub.add_parser("prune", help="Remove old cached videos")
    cache_prune_p.add_argument("--older-than", default="7d", help="Age threshold, e.g. 7d / 12h (default: 7d)")
    cache_prune_p.add_argument("--base", type=Path, default=None, help="Base dir containing yt-cache/ (default: cwd)")
    cache_prune_p.set_defaults(func=cmd_cache_prune)

    doctor_p = sub.add_parser(
        "doctor",
        help="Preflight the environment: what is present, what is missing, what to run next",
    )
    doctor_p.add_argument(
        "--json", action="store_true", help="Machine-readable report (same semantics)"
    )
    doctor_p.add_argument(
        "--base", type=Path, default=None, help="Base dir containing yt-cache/ (default: cwd)"
    )
    doctor_p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    # ``cache list``/``prune`` print paths, which are data: force UTF-8 first, the
    # same convention every flow CLI follows ([[task:2798]] F2).
    force_utf8_streams()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
