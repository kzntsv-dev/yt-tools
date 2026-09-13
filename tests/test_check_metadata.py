"""``scripts/check-metadata.py`` — is the built artifact publishable and clean?

Two failure classes, both learned the hard way:

1. **A direct reference in ``Requires-Dist``.** PyPI answers
   ``400 Can't have direct dependency`` and the version number is spent.
   ``python -m build`` and ``twine check`` are both happy with it — nothing
   local catches it, which is exactly how release ``v0.23.0`` died on the tag
   ([[wiki:3592]], task:2824).
2. **Dev-repo meta inside the artifact.** ``AGENTS.md``, ``CLAUDE.md``,
   ``.mappa/``, ``.wiki/``, ``.tasks/``, ``.pi/`` live in the private dev repo
   only; the published sdist comes from a curated copy, and a copy-paste slip
   would publish them.

Both are properties of the *artifact*, not of the source tree, so this runs on
what ``python -m build`` produced — once in CI, again in the release job. The
source-level half of (1) is ``tests/test_packaging.py``; this is the half that
sees what actually landed in the archives.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-metadata.py"

CLEAN_REQUIRES = (
    "youtube-transcript-api>=1.0.0",
    'scenedetect[opencv]>=0.6.4 ; extra == "frames"',
    'librosa>=0.11.0,<1.0 ; extra == "full"',
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _wheel(dist: Path, requires: tuple[str, ...] = CLEAN_REQUIRES, extra_files: tuple[str, ...] = ()) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / "yt_tools_cli-1.2.3-py3-none-any.whl"
    metadata = "\n".join(
        [
            "Metadata-Version: 2.3",
            "Name: yt-tools-cli",
            "Version: 1.2.3",
            *(f"Requires-Dist: {spec}" for spec in requires),
            "",
            "",
        ]
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("yt_tools_cli-1.2.3.dist-info/METADATA", metadata)
        zf.writestr("yt_tools_cli-1.2.3.dist-info/entry_points.txt", "[console_scripts]\n")
        zf.writestr("yt_tools/__init__.py", "")
        for name in extra_files:
            zf.writestr(name, "x")
    return path


def _sdist(dist: Path, requires: tuple[str, ...] = CLEAN_REQUIRES, extra_files: tuple[str, ...] = ()) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / "yt_tools_cli-1.2.3.tar.gz"
    pkg_info = "\n".join(
        [
            "Metadata-Version: 2.3",
            "Name: yt-tools-cli",
            "Version: 1.2.3",
            *(f"Requires-Dist: {spec}" for spec in requires),
            "",
            "",
        ]
    )
    with tarfile.open(path, "w:gz") as tf:
        def add(name: str, text: str) -> None:
            import io

            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        add("yt_tools_cli-1.2.3/PKG-INFO", pkg_info)
        add("yt_tools_cli-1.2.3/yt_tools/__init__.py", "")
        for name in extra_files:
            add(name, "x")
    return path


# ---- publishable metadata ---------------------------------------------------


def test_clean_artifacts_pass(tmp_path):
    dist = tmp_path / "dist"
    _wheel(dist)
    _sdist(dist)
    proc = _run(str(dist))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout.lower()


def test_direct_reference_in_the_wheel_fails_and_names_it(tmp_path):
    dist = tmp_path / "dist"
    _wheel(dist, requires=(*CLEAN_REQUIRES, 'bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0 ; extra == "full"'))
    proc = _run(str(dist))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "bpm-detector" in proc.stdout
    assert "git+" in proc.stdout


def test_direct_reference_in_the_sdist_fails(tmp_path):
    dist = tmp_path / "dist"
    _sdist(dist, requires=("librosa @ https://example.invalid/librosa.whl",))
    proc = _run(str(dist))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "librosa" in proc.stdout


# ---- no dev-repo meta inside the artifact -----------------------------------


def test_meta_file_in_the_sdist_fails_and_names_it(tmp_path):
    dist = tmp_path / "dist"
    _sdist(dist, extra_files=("yt_tools_cli-1.2.3/AGENTS.md",))
    proc = _run(str(dist))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "AGENTS.md" in proc.stdout


def test_meta_directory_in_the_sdist_fails(tmp_path):
    dist = tmp_path / "dist"
    _sdist(dist, extra_files=("yt_tools_cli-1.2.3/.mappa/share/manifest.json", ".pi/settings.json"))
    proc = _run(str(dist))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert ".mappa" in proc.stdout or ".pi/" in proc.stdout


def test_the_plugin_manifest_is_not_meta(tmp_path):
    """`.claude-plugin/plugin.json` is a shipped artifact — a false positive here
    would push someone to "fix" the release by deleting it."""
    dist = tmp_path / "dist"
    _sdist(dist, extra_files=("yt_tools_cli-1.2.3/.claude-plugin/plugin.json",))
    proc = _run(str(dist))
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- an unrunnable check is never a pass ------------------------------------


def test_missing_dist_directory_is_rc2(tmp_path):
    proc = _run(str(tmp_path / "nope"))
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_empty_dist_directory_is_rc2(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    proc = _run(str(dist))
    assert proc.returncode == 2, proc.stdout + proc.stderr
