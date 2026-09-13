#!/usr/bin/env python3
"""Is the built artifact publishable - and free of dev-repo meta?

Two failure classes, both met the hard way:

1. **A direct reference in ``Requires-Dist``.** PyPI rejects PEP 508 direct
   references with ``400 Can't have direct dependency`` and the version number
   is spent. ``python -m build`` and ``twine check`` both accept them, so
   nothing local says a word: release ``v0.23.0`` (2026-09-13) built green,
   passed ``twine check``, then died at the index on
   ``bpm-detector @ git+https://… ; extra == "full"``.
2. **Dev-repo meta inside the artifact.** ``AGENTS.md``, ``CLAUDE.md``,
   ``.mappa/``, ``.wiki/``, ``.tasks/``, ``.pi/`` belong to the private dev
   repo. The published sdist is built from a curated copy, so a copy-paste slip
   would publish them, and nothing in the build would notice.

Both are properties of the *artifact*, not of the source tree — which is why
this reads the archives instead of trusting ``pyproject.toml``. The source-level
half lives in ``tests/test_packaging.py``; this is the half that sees what
actually ships. CI runs it on every push, and the release job runs it again on
the very artifacts it is about to upload.

Exit codes: ``0`` clean · ``1`` problems found · ``2`` the check could not run
(no directory, no archives — an unrunnable check is never a pass).

Usage::

    python scripts/check-metadata.py            # ./dist
    python scripts/check-metadata.py /tmp/ytbuild
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

#: A PEP 508 direct reference: `name @ <scheme>…`. Same rule as the source-level
#: guard in tests/test_packaging.py — kept in sync by hand, both are three lines.
DIRECT_REF_RE = re.compile(
    r"@\s*(?:git\+|hg\+|svn\+|bzr\+|https?://|file:|ssh://)", re.IGNORECASE
)

#: Path *segments* that only exist in the private dev repo. Matched per segment,
#: so `.claude-plugin/plugin.json` (a shipped artifact) is not a hit while
#: `.mappa/share/manifest.json` and a bare `.pi/settings.json` are.
META_SEGMENTS = frozenset({".mappa", ".wiki", ".tasks", ".pi", "AGENTS.md", "CLAUDE.md", ".mappa-manifest.json"})

_REQUIRES_RE = re.compile(r"^Requires-Dist:\s*(.+?)\s*$", re.MULTILINE)


def _requires_from(metadata_text: str) -> list[str]:
    return [m.group(1) for m in _REQUIRES_RE.finditer(metadata_text)]


def _is_meta_path(name: str) -> str | None:
    parts = [part for part in name.split("/") if part]
    for part in parts:
        if part in META_SEGMENTS:
            return part
    return None


def _wheel_report(archive: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        metadata = next(
            (n for n in names if n.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata is None:
            return [f"{archive.name}: no *.dist-info/METADATA - not a well-formed wheel"], 0
        requires = _requires_from(zf.read(metadata).decode("utf-8", "replace"))
    for spec in requires:
        if DIRECT_REF_RE.search(spec):
            problems.append(f"{archive.name}: direct reference in Requires-Dist: {spec}")
    for name in names:
        leaked = _is_meta_path(name)
        if leaked:
            problems.append(f"{archive.name}: dev-repo meta in the wheel: {name}")
    return problems, len(requires)


def _sdist_report(archive: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    requires: list[str] = []
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        # `PKG-INFO` sits at <name>-<version>/PKG-INFO; older layouts used
        # <name>.egg-info/PKG-INFO. Take the shortest match — the top-level one.
        candidates = [m for m in members if m.name.endswith("PKG-INFO") or m.name.endswith("METADATA")]
        if not candidates:
            return [f"{archive.name}: no PKG-INFO/METADATA - not a well-formed sdist"], 0
        chosen = min(candidates, key=lambda m: m.name.count("/"))
        extracted = tf.extractfile(chosen)
        if extracted is not None:
            requires = _requires_from(io.TextIOWrapper(extracted, encoding="utf-8", errors="replace").read())
        for member in members:
            leaked = _is_meta_path(member.name)
            if leaked:
                problems.append(f"{archive.name}: dev-repo meta in the sdist: {member.name}")
    for spec in requires:
        if DIRECT_REF_RE.search(spec):
            problems.append(f"{archive.name}: direct reference in Requires-Dist: {spec}")
    return problems, len(requires)


def check(dist_dir: Path) -> int:
    print(f"metadata check: {dist_dir}")
    if not dist_dir.is_dir():
        print(f"  cannot run: {dist_dir} is not a directory (build first: python -m build)")
        return 2
    archives = sorted([*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz")])
    if not archives:
        print(f"  cannot run: no wheel or sdist in {dist_dir} (build first: python -m build)")
        return 2

    problems: list[str] = []
    for archive in archives:
        if archive.suffix == ".whl":
            found, count = _wheel_report(archive)
        else:
            found, count = _sdist_report(archive)
        problems.extend(found)
        state = "FAIL" if found else "ok  "
        print(f"  {state} {archive.name} ({count} Requires-Dist line(s))")
        for line in found:
            print(f"       - {line}")

    if problems:
        print(f"verdict: not publishable - {len(problems)} problem(s)")
        print("  fix: direct references cannot be published (install them outside the")
        print("       metadata); dev-repo meta must never reach a curated copy.")
        return 1
    print("verdict: publishable - no direct references, no dev-repo meta")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "dist_dir",
        nargs="?",
        default="dist",
        help="directory holding the built wheel/sdist (default: ./dist)",
    )
    args = parser.parse_args(argv)
    return check(Path(args.dist_dir))


if __name__ == "__main__":
    sys.exit(main())
