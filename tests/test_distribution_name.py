"""One distribution name, three places that must agree.

`yt-tools` cannot be the PyPI distribution name — it folds into the same
similarity class as the existing `yttools`, so PyPI refuses it ([[wiki:3589]]).
The distribution is `yt-tools-cli` while the *project*, the repo, the plugin and
the console commands stay `yt-tools`.

That split is a footgun: the name is written in `pyproject.toml` (what gets
uploaded), in the version reader (which reads install metadata *by name*) and in
the install advice every refusal prints (`pipx inject <name> …`). A rename that
touches one and not the others ships a package nobody can find, or advice that
names a package that does not exist. These tests pin the three together.
"""

from __future__ import annotations

import re
from pathlib import Path

from yt_tools import DISTRIBUTION, extras

ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r'^name\s*=\s*"([^"]+)"', re.MULTILINE)


def _pyproject_name() -> str:
    return NAME_RE.search((ROOT / "pyproject.toml").read_text(encoding="utf-8")).group(1)


def test_pyproject_declares_the_same_name_the_code_uses():
    """The uploaded name *is* the declared one: they cannot drift."""
    assert _pyproject_name() == DISTRIBUTION


def test_the_distribution_is_not_the_unregisterable_plain_name():
    """`yt-tools` is refused by PyPI's similarity rule — keep it out of metadata.

    The project name stays `yt-tools` everywhere a human reads it; only the
    distribution (what `pip` resolves, what PyPI accepts) is `yt-tools-cli`.
    """
    assert DISTRIBUTION == "yt-tools-cli"
    assert extras.inject_command("frames").startswith(f"pipx inject {DISTRIBUTION} ")
    assert f"pip install '{DISTRIBUTION}[frames]'" in extras._install_lines("frames")


def test_the_version_reader_asks_for_the_distribution_by_that_name(monkeypatch):
    """`metadata.version()` is name-based: a stale name silently falls back to
    pyproject and hides the desync until install time."""
    import importlib.metadata as metadata

    seen: list[str] = []

    def fake_version(name):
        seen.append(name)
        return "9.9.9"

    monkeypatch.setattr(metadata, "version", fake_version)
    import yt_tools

    yt_tools._installed_version.cache_clear()
    assert yt_tools._installed_version() == "9.9.9"
    yt_tools._installed_version.cache_clear()
    assert seen == [DISTRIBUTION]


def test_the_plugin_hook_targets_the_distribution_not_the_project():
    """The SessionStart hook probes and uninstalls by package name; if it kept
    `yt-tools` it would neither see an installed `yt-tools-cli` nor update it.
    """
    for script, probe in (
        (ROOT / "scripts" / "ensure-install.sh", "^yt-tools-cli "),
        (ROOT / "scripts" / "ensure-install.ps1", "^yt-tools-cli\\s+"),
    ):
        text = script.read_text(encoding="utf-8")
        assert probe in text, f"{script.name} does not probe {DISTRIBUTION}"
        assert f"uninstall {DISTRIBUTION}" in text, f"{script.name} uninstalls the wrong name"
        assert "uninstall yt-tools " not in text
