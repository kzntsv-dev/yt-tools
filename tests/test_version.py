"""One version, one source — pyproject is the canon ([[requirements:46]] D9, AC6).

`pyproject.toml` holds the version. `.claude-plugin/plugin.json` and the
`skills/using-yt-tools/SKILL.md` frontmatter are *derived*: they are written by
`scripts/sync-version.py` on release, never by hand. `yt_tools.__version__` is
not a constant either — it reads the installed distribution metadata, with a
source-tree fallback.

A divergence between any two of them is a release bug (the plugin marketplace
would advertise one version while pipx installs another), so the guard test
below fails on it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.version import Version

import yt_tools
from yt_tools import _installed_version, _resolve_version, version_from_pyproject

ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "scripts" / "sync-version.py"

PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
SKILL_MD = ROOT / "skills" / "using-yt-tools" / "SKILL.md"

_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _pyproject_version(root: Path = ROOT) -> str:
    m = _VERSION_RE.search((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert m, "pyproject.toml has no version = \"X.Y.Z\" line"
    return m.group(1)


def _plugin_json_version(path: Path = PLUGIN_JSON) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def _skill_frontmatter_version(path: Path = SKILL_MD) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with a YAML frontmatter block"
    block = text.split("---\n", 2)[1]
    m = re.search(r"^version:\s*(\S+)$", block, re.MULTILINE)
    assert m, "SKILL.md frontmatter has no version:"
    return m.group(1)


# ---- AC6: the guard ---------------------------------------------------------


def test_every_version_source_agrees():
    versions = {
        "pyproject.toml": _pyproject_version(),
        ".claude-plugin/plugin.json": _plugin_json_version(),
        "skills/using-yt-tools/SKILL.md": _skill_frontmatter_version(),
    }
    assert len(set(versions.values())) == 1, f"version divergence: {versions}"


def test_repo_passes_the_release_check():
    proc = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"sync-version.py --check failed:\n{proc.stdout}{proc.stderr}"


def test_release_version_is_valid_semver():
    Version(_pyproject_version())  # raises InvalidVersion on garbage


# ---- AC6: `__version__` is not a hand-written constant ----------------------


@pytest.fixture(autouse=True)
def _fresh_distribution_lookup():
    """The lookup is cached (F6) — a test that re-stubs metadata must start clean."""
    _installed_version.cache_clear()
    yield
    _installed_version.cache_clear()


def test_resolve_version_prefers_installed_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    stale_pyproject = tmp_path / "pyproject.toml"
    stale_pyproject.write_text('[project]\nversion = "1.1.1"\n', encoding="utf-8")
    assert _resolve_version(stale_pyproject) == "9.9.9"


def test_resolve_version_falls_back_to_the_tree(monkeypatch, tmp_path):
    """Not installed at all (a source checkout): the tree is the only source."""
    def _missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "version", _missing)
    stale_pyproject = tmp_path / "pyproject.toml"
    stale_pyproject.write_text('[project]\nversion = "1.1.1"\n', encoding="utf-8")
    assert _resolve_version(stale_pyproject) == "1.1.1"


def test_version_from_pyproject_reads_the_file(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "yt-tools"\nversion = "4.5.6"\n', encoding="utf-8")
    assert version_from_pyproject(pyproject) == "4.5.6"


def test_version_from_pyproject_is_none_when_unreadable(tmp_path):
    assert version_from_pyproject(tmp_path / "absent.toml") is None
    garbage = tmp_path / "pyproject.toml"
    garbage.write_text("[project]\nname = 'yt-tools'\n", encoding="utf-8")
    assert version_from_pyproject(garbage) is None


def test_package_version_matches_the_canon():
    """Whatever route `__version__` took, it must equal pyproject in this tree.

    In a source checkout that means the fallback; in an installed venv the
    metadata, which the packaging guard tests pin to the same number.
    """
    assert yt_tools.__version__ == _pyproject_version()


# ---- F5 ([[task:2799]]): the version reader is public, not a private boundary --


def test_the_pyproject_reader_is_public_and_declared(tmp_path):
    """``doctor`` cross-checks the same file, so the name is a module contract."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "7.7.7"\n', encoding="utf-8")

    assert "version_from_pyproject" in yt_tools.__all__
    assert version_from_pyproject(pyproject) == "7.7.7"


def test_nothing_outside_the_package_root_touches_a_private_version_name():
    """A private import is a boundary that drifts silently — the F5 finding."""
    offenders = [
        path.name
        for path in (ROOT / "yt_tools").glob("*.py")
        if path.name != "__init__.py" and "_version_from_pyproject" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"private version reader imported by: {offenders}"


# ---- F6 ([[task:2799]]): the metadata lookup is lazy and paid at most once ----


def test_the_distribution_lookup_is_paid_once(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(metadata, "version", lambda name: calls.append(name) or "9.9.9")

    assert _installed_version() == "9.9.9"
    assert _installed_version() == "9.9.9"
    assert _resolve_version() == "9.9.9"
    assert _resolve_version(Path("absent-pyproject.toml")) == "9.9.9"

    assert calls == ["yt-tools"], "the distribution lookup must not repeat in one process"


def test_importing_yt_tools_does_not_pay_for_importlib_metadata():
    """~130 ms of `import yt_tools` was `importlib.metadata`, paid by *every* CLI.

    Only whoever asks for the version needs it, so the import moves inside the
    lookup (PEP 562 attribute on demand) — and asking must still work.
    """
    probe = (
        "import json, sys\n"
        "import yt_tools\n"
        "before = 'importlib.metadata' in sys.modules\n"
        "version = yt_tools.__version__\n"
        "print(json.dumps({\n"
        "    'before': before,\n"
        "    'after': 'importlib.metadata' in sys.modules,\n"
        "    'version': version,\n"
        "    'all': yt_tools.__all__,\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["before"] is False, "importing the package must not import importlib.metadata"
    assert report["after"] is True, "asking for the version must resolve it"
    assert report["version"] == _pyproject_version()
    assert "__version__" in report["all"]


# ---- the sync script itself -------------------------------------------------


def _mini_project(root: Path, *, package: str = "0.15.1", plugin: str = "0.13.1", skill: str = "0.6.0") -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "using-yt-tools").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "yt-tools"\nversion = "{package}"\n', encoding="utf-8"
    )
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "yt-tools", "version": plugin, "skills": "./skills/"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "skills" / "using-yt-tools" / "SKILL.md").write_text(
        f"---\nname: using-yt-tools\nversion: {skill}\ndescription: text\n---\n\n# body\n",
        encoding="utf-8",
    )


def _run_sync(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


def test_sync_check_fails_on_divergence_and_names_the_file(tmp_path):
    _mini_project(tmp_path)
    proc = _run_sync(tmp_path, "--check")
    assert proc.returncode != 0
    assert "plugin.json" in proc.stdout + proc.stderr
    assert "SKILL.md" in proc.stdout + proc.stderr


def test_sync_writes_both_targets_from_pyproject(tmp_path):
    _mini_project(tmp_path)
    proc = _run_sync(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _plugin_json_version(tmp_path / ".claude-plugin" / "plugin.json") == "0.15.1"
    assert _skill_frontmatter_version(tmp_path / "skills" / "using-yt-tools" / "SKILL.md") == "0.15.1"
    # And the check now passes.
    assert _run_sync(tmp_path, "--check").returncode == 0


def test_sync_keeps_the_rest_of_the_files_intact(tmp_path):
    _mini_project(tmp_path)
    _run_sync(tmp_path)
    plugin = json.loads((tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "yt-tools"
    assert plugin["skills"] == "./skills/"
    skill = (tmp_path / "skills" / "using-yt-tools" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: using-yt-tools\nversion: 0.15.1\ndescription: text\n---\n\n# body\n")


def test_sync_is_idempotent(tmp_path):
    _mini_project(tmp_path)
    _run_sync(tmp_path)
    first = (tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    _run_sync(tmp_path)
    assert (tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8") == first


def test_sync_refuses_when_pyproject_has_no_version(tmp_path):
    _mini_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "yt-tools"\n', encoding="utf-8")
    proc = _run_sync(tmp_path)
    assert proc.returncode != 0
    assert "version" in (proc.stdout + proc.stderr).lower()
