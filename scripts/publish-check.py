#!/usr/bin/env python3
"""Did the release actually land on PyPI?

A green build is not a published release. ``sched`` learned this the expensive
way: a wave failed on the ``tsc``/dist step and nobody noticed until an external
check looked — twice for the same version (their ``scripts/publish-check.mjs``).
The question "is the version people install the version we tagged?" belongs to a
machine, and it belongs *after* the upload.

So this runs after the publish step and answers three things:

1. does the distribution exist on the index at all ("never landed" — a 404 body);
2. is its latest version the one this checkout declares ("version drift" — the
   build published something else, or a stale tag);
3. are both artifacts there (a release that landed half-built is not a release).

Those are three different failures with three different fixes, so they are
reported separately. Exit codes: ``0`` landed · ``1`` not landed / drifted /
incomplete · ``2`` the check could not run (an unrunnable check is never a pass).

Usage::

    python scripts/publish-check.py                 # version from pyproject.toml
    python scripts/publish-check.py --version 0.23.0
    python scripts/publish-check.py --json-file fixture.json   # offline / tests
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_NAME_RE = re.compile(r'^name\s*=\s*"([^"]+)"', re.MULTILINE)

#: Both artifact kinds a release is expected to ship.
EXPECTED_KINDS = ("bdist_wheel", "sdist")


def read_pyproject(path: Path) -> tuple[str | None, str | None]:
    """``(name, version)`` from ``pyproject.toml``, or ``(None, None)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    name = _NAME_RE.search(text)
    version = _VERSION_RE.search(text)
    return (name.group(1) if name else None, version.group(1) if version else None)


def fetch_payload(name: str, url: str | None) -> dict:
    with urllib.request.urlopen(url or PYPI_JSON.format(name=name), timeout=60) as response:
        return json.load(response)


def check(payload: dict, expected_name: str, expected_version: str) -> tuple[int, list[str]]:
    lines = [f"publish check: {expected_name} {expected_version}"]

    if "info" not in payload:
        lines.append("  verdict: does not exist on the index - the release never landed")
        lines.append("  fix    : wait for the publish job, or re-run it; the upload itself failed")
        return 1, lines

    info = payload.get("info", {})
    published_name = info.get("name", "?")
    published_version = info.get("version", "?")
    lines.append(f"  on the index: {published_name} {published_version}")

    if published_name.lower().replace("_", "-") != expected_name.lower().replace("_", "-"):
        lines.append(f"  verdict: name drift - expected {expected_name!r}, index has {published_name!r}")
        return 1, lines

    if published_version != expected_version:
        lines.append(f"  verdict: version drift - tagged {expected_version}, installable {published_version}")
        lines.append("  fix    : the upload published another version; check the tag and the build")
        return 1, lines

    kinds = {f.get("packagetype") for f in payload.get("urls", [])}
    missing = [kind for kind in EXPECTED_KINDS if kind not in kinds]
    if missing:
        lines.append(f"  artifacts: {', '.join(sorted(k for k in kinds if k)) or 'none'}")
        lines.append(f"  verdict: incomplete - missing {', '.join(missing)}")
        lines.append("  fix    : the publish step uploaded a partial release")
        return 1, lines

    lines.append(f"  artifacts: {', '.join(sorted(k for k in kinds if k))}")
    lines.append("  verdict: landed - what people install is what we tagged")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish-check",
        description="Verify that a release landed on PyPI (run after the publish step).",
    )
    parser.add_argument("--name", help="distribution name (default: pyproject.toml)")
    parser.add_argument("--version", help="expected version (default: pyproject.toml)")
    parser.add_argument("--json-file", type=Path, help="read the index payload from a file (offline)")
    parser.add_argument("--url", help="override the JSON API URL")
    args = parser.parse_args(argv)

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared_name, declared_version = read_pyproject(pyproject)
    name = args.name or declared_name
    version = args.version or declared_version
    if not name or not version:
        print(f"error: no name/version in {pyproject} and none given", file=sys.stderr)
        return 2

    if args.json_file is not None:
        try:
            payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read {args.json_file}: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            payload = fetch_payload(name, args.url)
        except urllib.error.HTTPError as exc:
            # A 404 is a *result* (nothing landed), not a broken check.
            if exc.code == 404:
                payload = {"message": "Not Found"}
            else:
                print(f"error: the index answered {exc.code} for {name}", file=sys.stderr)
                return 2
        except (urllib.error.URLError, OSError) as exc:
            print(f"error: cannot reach the index for {name}: {exc}", file=sys.stderr)
            return 2

    code, lines = check(payload, name, version)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
