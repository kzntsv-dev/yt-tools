#!/usr/bin/env python3
"""Sync the release version from ``pyproject.toml`` into the derived manifests.

Canon: ``pyproject.toml``. Derived: ``.claude-plugin/plugin.json`` and the
``skills/using-yt-tools/SKILL.md`` frontmatter ([[requirements:46]] D9). None of
the derived files is edited by hand — a release is:

    bump ``version`` in pyproject.toml
    python scripts/sync-version.py
    python scripts/sync-version.py --check     # what the guard test runs
    git commit -m "feat(...): ... (vX.Y.Z)"

``--check`` never writes: it reports the divergence and the fix, so CI / the
test suite can fail on drift without mutating the tree.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
FRONTMATTER_VERSION_RE = re.compile(r"^(version:\s*)(\S+)$", re.MULTILINE)

PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"
SKILL_MD = Path("skills") / "using-yt-tools" / "SKILL.md"


class SyncError(RuntimeError):
    """The canon is unreadable, or a derived file is not shaped as expected."""


# ---- canon ------------------------------------------------------------------


def read_canon_version(root: Path) -> str:
    path = root / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SyncError(f"cannot read {path}: {e}") from e
    m = VERSION_RE.search(text)
    if not m:
        raise SyncError(f'{path}: no version = "X.Y.Z" line — the canon is missing')
    return m.group(1)


# ---- derived readers --------------------------------------------------------


def read_plugin_json_version(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SyncError(f"cannot read {path}: {e}") from e
    version = data.get("version")
    if not isinstance(version, str):
        raise SyncError(f"{path}: no string version field")
    return version


def read_skill_version(path: Path) -> str:
    text = _read(path)
    block = _frontmatter(text, path)
    m = FRONTMATTER_VERSION_RE.search(block)
    if not m:
        raise SyncError(f"{path}: frontmatter has no version: line")
    return m.group(2)


# ---- derived writers --------------------------------------------------------


def write_plugin_json_version(path: Path, version: str) -> None:
    """Rewrite only the version, leaving key order and formatting alone."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SyncError(f"cannot read {path}: {e}") from e
    data["version"] = version
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_skill_version(path: Path, version: str) -> None:
    """Rewrite only the frontmatter version line — the body is not touched."""
    text = _read(path)
    block = _frontmatter(text, path)
    if not FRONTMATTER_VERSION_RE.search(block):
        raise SyncError(f"{path}: frontmatter has no version: line")
    new_block = FRONTMATTER_VERSION_RE.sub(
        lambda m: m.group(1) + version, block, count=1
    )
    # Rebuild around the *existing* delimiters so byte-identical input stays identical.
    parts = text.split("---\n", 2)
    parts[1] = new_block
    path.write_text("---\n".join(parts), encoding="utf-8")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise SyncError(f"cannot read {path}: {e}") from e


def _frontmatter(text: str, path: Path) -> str:
    if not text.startswith("---\n"):
        raise SyncError(f"{path}: no YAML frontmatter block")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise SyncError(f"{path}: unterminated frontmatter block")
    return parts[1]


# ---- driver -----------------------------------------------------------------


def collect(root: Path) -> dict[str, str]:
    return {
        str(PLUGIN_JSON): read_plugin_json_version(root / PLUGIN_JSON),
        str(SKILL_MD): read_skill_version(root / SKILL_MD),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync-version.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="project root (default: the repo this script lives in)")
    parser.add_argument("--check", action="store_true",
                        help="verify only; exit 1 on divergence, write nothing")
    args = parser.parse_args(argv)

    root: Path = args.root
    try:
        canon = read_canon_version(root)
        current = collect(root)
    except SyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    stale = {name: version for name, version in current.items() if version != canon}

    if args.check:
        if not stale:
            print(f"ok: version {canon} is in sync in {len(current)} derived file(s)")
            return 0
        divergence = ", ".join(f"{name} says {version}" for name, version in current.items())
        print(
            f"error: version divergence — pyproject.toml says {canon}, {divergence}\n"
            f"fix: python {Path(__file__).name}",
            file=sys.stderr,
        )
        return 1

    if not stale:
        print(f"ok: version {canon} already in sync — nothing to write")
        return 0

    for name in stale:
        path = root / name
        if name == str(PLUGIN_JSON):
            write_plugin_json_version(path, canon)
        else:
            write_skill_version(path, canon)
        print(f"{name}: {stale[name]} -> {canon}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
