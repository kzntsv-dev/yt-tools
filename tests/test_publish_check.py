"""``scripts/publish-check.py`` — did the release actually land on PyPI?

The lesson this encodes is sched's (`scripts/publish-check.mjs`): a wave published
twice without the artifact arriving, noticed only by an external check. A green
build is not a published release — the question "is the version people install
the version we tagged?" has to be asked by a machine, after the upload.

Offline-testable by design: ``--json-file`` feeds the same payload the JSON API
serves, so the verdict logic is pinned by tests instead of by the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish-check.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _payload(version: str, *, wheel: bool = True, sdist: bool = True) -> str:
    files = []
    if wheel:
        files.append({"filename": f"yt_tools_cli-{version}-py3-none-any.whl", "packagetype": "bdist_wheel"})
    if sdist:
        files.append({"filename": f"yt_tools_cli-{version}.tar.gz", "packagetype": "sdist"})
    return json.dumps({"info": {"name": "yt-tools-cli", "version": version}, "urls": files})


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pypi.json"
    path.write_text(text, encoding="utf-8")
    return path


def _pyproject_version() -> str:
    import re

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)


def test_a_landed_release_passes(tmp_path):
    expected = _pyproject_version()

    proc = _run("--json-file", str(_write(tmp_path, _payload(expected))))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert expected in proc.stdout


def test_a_version_that_never_landed_fails_and_names_both_versions(tmp_path):
    """The failure sched saw twice: build green, registry still on the old one."""
    proc = _run("--version", "9.9.9", "--json-file", str(_write(tmp_path, _payload("0.22.1"))))

    assert proc.returncode == 1
    assert "9.9.9" in proc.stdout  # what we tagged
    assert "0.22.1" in proc.stdout  # what the world installs


def test_a_missing_artifact_kind_fails(tmp_path):
    """A release can land half-built: wheel without sdist is not a release."""
    expected = _pyproject_version()

    proc = _run(
        "--json-file", str(_write(tmp_path, _payload(expected, sdist=False)))
    )

    assert proc.returncode == 1
    assert "sdist" in proc.stdout
    assert "bdist_wheel" not in proc.stdout.split("verdict")[0] or "sdist" in proc.stdout


def test_a_project_that_does_not_exist_yet_is_not_a_mismatch(tmp_path):
    """A 404 body must be reported as "never landed", not as a version drift —
    the two have different fixes (wait/re-run the upload vs bump the version)."""
    proc = _run("--json-file", str(_write(tmp_path, '{"message": "Not Found"}')))

    assert proc.returncode == 1
    assert "does not exist" in proc.stdout or "never landed" in proc.stdout


def test_the_default_expected_version_is_the_tree_s(tmp_path):
    """No arguments: check what this checkout says it is."""
    proc = _run("--json-file", str(_write(tmp_path, _payload(_pyproject_version()))))

    assert proc.returncode == 0
    assert _pyproject_version() in proc.stdout


def test_an_unrunnable_check_is_never_a_pass(tmp_path):
    proc = _run("--json-file", str(tmp_path / "nope.json"))

    assert proc.returncode == 2
    assert "nope.json" in proc.stderr
