"""yt-tools — fetch YouTube transcripts and frames for iterative agent-driven viewing.

``__version__`` is derived, never hand-written: the version lives in
``pyproject.toml`` and is synced into the plugin manifest and the skill
frontmatter by ``scripts/sync-version.py`` at release time ([[requirements:46]] D9).
It is also *lazy*: resolving it imports ``importlib.metadata`` (~130 ms of the
~145 ms ``import yt_tools``), and most CLIs never ask ([[task:2799]] F6).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

#: The **distribution** name — what ``pip``/``pipx`` resolve and what PyPI accepts.
#: Deliberately not the project name: ``yt-tools`` is unregisterable on PyPI (its
#: similarity folding collides with the existing ``yttools``, wiki:3589), while
#: the project, repo, plugin and every console command stay ``yt-tools``. Three
#: places must agree on this string — here (install metadata by name),
#: ``pyproject.toml`` (what gets uploaded) and :func:`yt_tools.extras.inject_command`
#: (the advice every refusal prints); ``tests/test_distribution_name.py`` pins them.
DISTRIBUTION = "yt-tools-cli"

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
UNKNOWN_VERSION = "0.0.0+unknown"


def version_from_pyproject(path: Path) -> str | None:
    """Version declared in ``pyproject.toml``, or None when unreadable/absent.

    Public because ``doctor`` cross-checks the same file and must not reach
    across a private boundary ([[task:2799]] F5).

    Deliberately a regex rather than TOML: the fallback must work on Python 3.10,
    where ``tomllib`` is not in the stdlib, and this module must not grow a
    dependency of its own.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


@lru_cache(maxsize=None)
def _installed_version() -> str | None:
    """The installed distribution's version, or None when it is not installed.

    ``importlib.metadata`` is imported *here*, not at module level: it was ~130 ms
    of the ~145 ms ``import yt_tools``, paid by every CLI, while only whoever asks
    for the version needs it ([[task:2799]] F6). The answer is cached because it
    cannot change inside a running process — a reinstall is a new interpreter.
    Tests that re-stub ``metadata.version`` clear it.
    """
    from importlib import metadata

    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None


def _resolve_version(pyproject_path: Path | None = None) -> str:
    """The installed distribution's version, or the tree's pyproject when run from source."""
    return (
        _installed_version()
        or version_from_pyproject(pyproject_path or _PYPROJECT)
        or UNKNOWN_VERSION
    )


def __getattr__(name: str) -> str:
    """``__version__`` on first *use*, not on every ``import yt_tools`` (F6)."""
    if name == "__version__":
        return _resolve_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), "__version__"})


__all__ = ["DISTRIBUTION", "__version__", "UNKNOWN_VERSION", "version_from_pyproject"]
