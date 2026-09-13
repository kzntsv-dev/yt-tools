#!/usr/bin/env python3
"""Did the release actually land on PyPI?

A green build is not a published release. ``sched`` learned this the expensive
way: a wave failed on the ``tsc``/dist step and nobody noticed until an external
check looked — twice for the same version (their ``scripts/publish-check.mjs``).
The question "is the version people install the version we tagged?" belongs to a
machine, and it belongs *after* the upload.

So this runs after the publish step and answers three things:

1. does *this* version exist on the index at all ("never landed" — a 404 body);
2. is its artifact set complete (a release that landed half-built is not a
   release);
3. if it is absent, did something else land instead ("version drift" — the build
   published another version, or a stale tag).

**Which payload answers which question matters.** The project-level endpoint
``/pypi/<name>/json`` is CDN-cached (``cache-control: max-age=900``), so seconds
after a successful upload it still serves the *previous* version — and this check
runs seconds after a successful upload. Adjudicating on it produced a false
"version drift" on release 0.23.1 for a release that had landed ([[task:2826]],
the whole reason this file asks twice). The version-scoped endpoint
``/pypi/<name>/<version>/json`` is a URL nobody has fetched before, so it cannot
be stale: it decides. The project endpoint only ever explains.

Exit codes: ``0`` landed · ``1`` not landed / drifted / incomplete · ``2`` the
check could not run (an unrunnable check is never a pass).

Usage::

    python scripts/publish-check.py                 # version from pyproject.toml
    python scripts/publish-check.py --version 0.23.1
    # offline / tests: one file per endpoint
    python scripts/publish-check.py --json-file version.json --project-json-file project.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: The version-scoped endpoint: unique per release, therefore never stale.
PYPI_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"
#: The project-scoped endpoint: what `latest` means to a user, and CDN-cached.
PYPI_PROJECT_JSON = "https://pypi.org/pypi/{name}/json"
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_NAME_RE = re.compile(r'^name\s*=\s*"([^"]+)"', re.MULTILINE)

#: Both artifact kinds a release is expected to ship.
EXPECTED_KINDS = ("bdist_wheel", "sdist")

NOT_FOUND: dict = {"message": "Not Found"}


class CheckError(RuntimeError):
    """The check could not run (bad fixture, unreachable index, HTTP != 404)."""


def read_pyproject(path: Path) -> tuple[str | None, str | None]:
    """``(name, version)`` from ``pyproject.toml``, or ``(None, None)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    name = _NAME_RE.search(text)
    version = _VERSION_RE.search(text)
    return (name.group(1) if name else None, version.group(1) if version else None)


def read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"cannot read {path}: {exc}") from exc


def fetch_payload(url: str) -> dict:
    """The index payload, or ``NOT_FOUND`` when the index says 404.

    A 404 is a *result* (nothing is there), not a broken check — that distinction
    is the difference between "wait for the upload" and "the check is unusable".
    """
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return dict(NOT_FOUND)
        raise CheckError(f"the index answered {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"cannot reach the index at {url}: {exc}") from exc


def _canonical(name: str) -> str:
    return name.lower().replace("_", "-")


def _project_note(project_payload: dict | None, expected_version: str) -> list[str]:
    """Explain a lagging project endpoint — never decide with it."""
    if not project_payload or "info" not in project_payload:
        return []
    latest = project_payload["info"].get("version")
    if not latest or latest == expected_version:
        return []
    return [
        f"  note: the project endpoint still serves {latest} - PyPI's project JSON API is "
        "CDN-cached (cache-control: max-age=900), so it can lag minutes behind an upload; "
        "the version endpoint above is the authority"
    ]


def _not_landed(
    lines: list[str], project_payload: dict | None, expected_version: str
) -> tuple[int, list[str]]:
    """Our version is absent — the project endpoint says whether something else landed."""
    if project_payload and "info" in project_payload:
        info = project_payload["info"]
        latest = info.get("version")
        lines.append(f"  on the project endpoint: {info.get('name', '?')} {latest}")
        if latest and latest != expected_version:
            lines.append(f"  verdict: version drift - tagged {expected_version}, installable {latest}")
            lines.append("  fix    : the upload published another version; check the tag and the build")
            return 1, lines
    lines.append("  verdict: never landed - the release is not on the index")
    lines.append("  fix    : wait for the publish job, or re-run it; the upload itself failed")
    return 1, lines


def check(
    version_payload: dict,
    project_payload: dict | None,
    expected_name: str,
    expected_version: str,
) -> tuple[int, list[str]]:
    lines = [f"publish check: {expected_name} {expected_version}"]

    if "info" not in version_payload:
        return _not_landed(lines, project_payload, expected_version)

    info = version_payload.get("info", {})
    published_name = info.get("name", "?")
    published_version = info.get("version", "?")
    lines.append(f"  on the version endpoint: {published_name} {published_version}")

    if _canonical(published_name) != _canonical(expected_name):
        lines.append(f"  verdict: name drift - expected {expected_name!r}, index has {published_name!r}")
        return 1, lines

    if published_version != expected_version:
        lines.append(
            f"  verdict: version drift - tagged {expected_version}, "
            f"that endpoint serves {published_version}"
        )
        lines.append("  fix    : the upload published another version; check the tag and the build")
        return 1, lines

    kinds = {f.get("packagetype") for f in version_payload.get("urls", [])}
    missing = [kind for kind in EXPECTED_KINDS if kind not in kinds]
    if missing:
        lines.append(f"  artifacts: {', '.join(sorted(k for k in kinds if k)) or 'none'}")
        lines.append(f"  verdict: incomplete - missing {', '.join(missing)}")
        lines.append("  fix    : the publish step uploaded a partial release")
        return 1, lines

    lines.append(f"  artifacts: {', '.join(sorted(k for k in kinds if k))}")
    lines.extend(_project_note(project_payload, expected_version))
    lines.append("  verdict: landed - what people install is what we tagged")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish-check",
        description="Verify that a release landed on PyPI (run after the publish step).",
    )
    parser.add_argument("--name", help="distribution name (default: pyproject.toml)")
    parser.add_argument("--version", help="expected version (default: pyproject.toml)")
    parser.add_argument("--json-file", type=Path, help="read the version-endpoint payload from a file (offline)")
    parser.add_argument(
        "--project-json-file",
        type=Path,
        help="read the project-endpoint payload from a file (offline; optional)",
    )
    parser.add_argument("--url", help="override the version-endpoint URL")
    parser.add_argument("--project-url", help="override the project-endpoint URL")
    args = parser.parse_args(argv)

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared_name, declared_version = read_pyproject(pyproject)
    name = args.name or declared_name
    version = args.version or declared_version
    if not name or not version:
        print(f"error: no name/version in {pyproject} and none given", file=sys.stderr)
        return 2

    try:
        if args.json_file is not None:
            version_payload = read_json_file(args.json_file)
            project_payload = (
                read_json_file(args.project_json_file) if args.project_json_file is not None else None
            )
        else:
            version_payload = fetch_payload(
                args.url or PYPI_VERSION_JSON.format(name=name, version=version)
            )
            project_url = args.project_url or PYPI_PROJECT_JSON.format(name=name)
            try:
                project_payload = fetch_payload(project_url)
            except CheckError:
                # Only needed to *explain* an absent version; a proven landing
                # must not fail because the advisory probe hiccuped.
                if "info" in version_payload:
                    project_payload = None
                else:
                    raise
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    code, lines = check(version_payload, project_payload, name, version)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
