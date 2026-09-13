"""``scripts/check-pypi-name.py`` — the pre-flight against PyPI's name rules.

Why this exists at all: a 404 on ``pypi.org/pypi/<name>/json`` means "no project
with that name", which is *not* the same as "the name is available". Warehouse
also rejects a new project whose name is **too similar** to an existing one, by
comparing ``ultranormalize_name()`` — a lossy SQL function that drops ``. _ -``
and folds ``l|L|i|I`` → ``1``, ``o|O`` → ``0``, then lowercases. ``yt-tools``
folds to ``ytt001s``, exactly like the existing YouTube toolkit ``yttools``, so
the obvious name is unregisterable — found the hard way (2026-09-13), which is
what this guard is for.

Contract: exit 1 when the name must be rejected (taken or too similar), exit 0
when it can be registered. The check is offline-testable via ``--names-file``,
so the rule itself is pinned by tests instead of by a live network call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-pypi-name.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _names_file(tmp_path: Path, names: list[str]) -> Path:
    path = tmp_path / "names.txt"
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    return path


# ---- the rule itself ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("yt-tools", "ytt001s"),
        ("yt_tools", "ytt001s"),
        ("yttools", "ytt001s"),
        ("yt-tools-cli", "ytt001sc11"),
        ("OI-distributions", "01d1str1but10ns"),
        ("yt-dlp", "ytd1p"),
    ],
)
def test_explain_reproduces_the_warehouse_folding_rule(name, expected, tmp_path):
    """The folding is the whole verdict, so it is pinned case by case.

    ``yt-tools`` vs ``yttools`` is the case that cost us the name: both fold to
    ``ytt001s``, and PyPI refuses the second registration of a folding class
    (``warehouse/packaging/services.py`` → ``ProjectNameUnavailableSimilarError``).
    """
    proc = _run("--explain", name)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


# ---- the verdict -------------------------------------------------------------


def test_a_name_that_folds_like_an_existing_one_is_refused(tmp_path):
    names = _names_file(tmp_path, ["yttools", "yt-dlp", "requests"])

    proc = _run("yt-tools", "--names-file", str(names))

    assert proc.returncode == 1
    assert "yttools" in proc.stdout  # names the culprit, unlike PyPI's own 400
    assert "too similar" in proc.stdout
    assert "ytt001s" in proc.stdout  # and the folding class it landed in


def test_the_reserved_name_passes_against_the_same_list(tmp_path):
    """``yt-tools-cli`` is the name we settled on precisely because it does not
    fold into ``yttools`` — this is that decision, executable."""
    names = _names_file(tmp_path, ["yttools", "yt-dlp", "requests"])

    proc = _run("yt-tools-cli", "--names-file", str(names))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "available" in proc.stdout


def test_a_name_that_already_exists_is_reported_as_taken_not_similar(tmp_path):
    """Taken and too-similar are different refusals; the fix differs (rename vs
    file a PEP 541 request), so the script must not blur them."""
    names = _names_file(tmp_path, ["yt-tools-cli", "yttools"])

    proc = _run("yt-tools-cli", "--names-file", str(names))

    assert proc.returncode == 1
    assert "already exists" in proc.stdout
    assert "too similar" not in proc.stdout


def test_the_default_name_is_the_one_we_reserved(tmp_path):
    """No argument = check what we actually intend to register."""
    names = _names_file(tmp_path, ["yttools"])

    proc = _run("--names-file", str(names))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "yt-tools-cli" in proc.stdout


def test_the_names_file_is_read_the_way_the_index_serves_it(tmp_path):
    """Real index lines are ``<a href=...>name</a>``; a plain list must work too,
    and blank lines / duplicates must not turn into phantom collisions."""
    path = tmp_path / "names.txt"
    path.write_text("requests\n\n  yttools  \nrequests\n", encoding="utf-8")

    proc = _run("yt-tools", "--names-file", str(path))

    assert proc.returncode == 1
    assert "yttools" in proc.stdout


def test_a_missing_names_file_fails_loudly(tmp_path):
    proc = _run("yt-tools-cli", "--names-file", str(tmp_path / "nope.txt"))

    assert proc.returncode == 2
    assert "nope.txt" in proc.stderr


def test_the_output_stays_ascii():
    """The same rule the CLI templates follow: a Windows console in cp1251 must not
    be able to break the check. Found the hard way — the verdict line printed an
    ellipsis, the subprocess harness decoded cp1251 bytes as UTF-8, and the test
    died on a None stdout instead of on its assertion.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if "print(" in line and any(ord(ch) > 127 for ch in line)
    ]
    assert not offenders, f"non-ASCII in printed output: {offenders}"


def test_ultranormalize_is_its_own_regex_and_not_a_near_miss():
    """Guard against a well-meaning rewrite (e.g. ``str.lower()`` only): the
    folding must still drop separators and fold the ambiguous glyphs."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "[._-]" in source, "separator stripping ([._-]) is gone"
    for folded in ("[li]", "o"):
        assert folded in source, f"folding rule for {folded!r} disappeared"
