"""``yt-tools doctor`` — preflight of the environment the flows run in.

Contract: [[requirements:46]] D5–D8. Doctor answers three questions in one
place: *what is present*, *what is missing*, and *what exactly to run next*.
It is the first command an agent (or a human) should run on a fresh machine —
"the environment explains itself" instead of failing three steps into a flow.

Two rules shape the report:

* **A check, not a verdict.** Every check carries ``status`` = ``ok`` |
  ``warn`` | ``missing``; the report carries ``can_proceed`` (bool) and
  ``next_step`` (an exact command). ``warn`` is a recommendation, never a
  blocker (a light flow still lives without ``[audio]``). Only the two external
  binaries the suite shells out to — ``yt-dlp`` and ``ffmpeg`` — block, because
  nothing at all works without them (D6).
* **Diagnosis only.** Doctor installs nothing and mutates nothing: it never
  creates the cache directory it inspects (D8). ``next_step`` is a command the
  caller may choose to run, not an action taken here.

Same semantics in both forms: the human text is the default, ``--json`` is the
machine form of the same report (D5/D7).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from yt_tools import __version__, version_from_pyproject
from yt_tools import extras
from yt_tools.cache import cache_list, cache_root, format_size
from yt_tools.core import force_utf8_streams

OK = "ok"
WARN = "warn"
MISSING = "missing"

# Keep in step with ``requires-python`` in pyproject.toml (README → Requirements).
PYTHON_MIN = (3, 10)
PYTHON_MAX_EXCLUSIVE = (3, 13)
PYTHON_RANGE = ">=3.10,<3.13"

# The exact per-OS command, mirroring README → Installation → ffmpeg.
FFMPEG_FIX = {
    "win32": "winget install Gyan.FFmpeg",
    "darwin": "brew install ffmpeg",
    "linux": "sudo apt install ffmpeg",
}
FFMPEG_FIX_FALLBACK = "install ffmpeg (see README -> Installation)"

# What each missing extra costs the caller — the reason `missing` here is
# informative rather than fatal.
EXTRA_FLOWS = {
    "frames": "yt-frames --mode scene and yt-watch",
    "audio": "yt-listen",
    "ocr": "yt-ocr",
}


@dataclass(frozen=True)
class Check:
    """One preflight check: what it found, and how to fix it if it can be fixed."""

    name: str
    status: str
    detail: str
    required: bool = False
    fix: str | None = None

    @property
    def blocking(self) -> bool:
        """Only a *required* check can block; ``warn`` never does."""
        return self.required and self.status == MISSING

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class Report:
    """The whole preflight: per-check detail plus the two contract fields."""

    checks: tuple[Check, ...]
    version: str = __version__

    @property
    def blockers(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.blocking)

    @property
    def can_proceed(self) -> bool:
        return not self.blockers

    @property
    def missing_extras(self) -> tuple[str, ...]:
        return tuple(
            c.name.split(":", 1)[1]
            for c in self.checks
            if c.name.startswith("extra:") and c.status == MISSING
        )

    @property
    def next_step(self) -> str | None:
        """The one exact command worth running next, or None when nothing is wrong.

        Priority: a blocker's fix (nothing else matters), then the extras that
        are absent (a recommendation — the light flows live without them), then
        any ``warn`` that happens to carry a fix. A blocker without a fix yields
        nothing: recommending an extras install while the environment is blocked
        would be worse than saying nothing ([[task:2799]] gap 8).
        """
        for check in self.blockers:
            if check.fix:
                return check.fix
        if self.blockers:
            return None
        if self.missing_extras:
            packages = " ".join(p for e in self.missing_extras for p in extras.EXTRA_PACKAGES[e])
            # Built from the same constant the refusals use: a rename that missed
            # this copy would make the report recommend a package that does not
            # exist ([[wiki:3589]]).
            return f"pipx inject {extras.DISTRIBUTION} {packages}"
        for check in self.checks:
            if check.status == WARN and check.fix:
                return check.fix
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "can_proceed": self.can_proceed,
            "next_step": self.next_step,
            "checks": [c.to_dict() for c in self.checks],
        }

    def render(self) -> str:
        lines = [f"yt-tools doctor - environment preflight (yt-tools {self.version})", ""]
        for check in self.checks:
            lines.append(f"  {check.status:<8} {check.name:<13} {check.detail}")
            if check.fix:
                lines.append(f"  {'':<8} {'':<13} -> {check.fix}")
        lines.append("")
        lines.append(f"can_proceed: {'yes' if self.can_proceed else 'no'}")
        lines.append(f"next_step: {self.next_step or '-'}")
        return "\n".join(lines)


# ---- individual checks -------------------------------------------------------


def _format_version(parts: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in parts[:3])


def check_python(version_info: tuple[int, ...] | None = None) -> Check:
    """Interpreter version against the declared support range.

    Out of range is a ``warn``, not a blocker: D6 names only ``yt-dlp`` and
    ``ffmpeg`` as blockers, and a doctor that runs at all proves the package
    imported on this interpreter.
    """
    parts = tuple(version_info if version_info is not None else sys.version_info)[:3]
    version = _format_version(parts)
    if parts < PYTHON_MIN:
        return Check(
            "python",
            WARN,
            f"{version} is below the supported range {PYTHON_RANGE} - reinstall with a supported interpreter",
        )
    if parts >= PYTHON_MAX_EXCLUSIVE:
        return Check(
            "python",
            WARN,
            f"{version} is above the supported range {PYTHON_RANGE} (untested; "
            f"the ceiling is librosa's Python 3.13 friction)",
        )
    return Check("python", OK, f"{version} ({PYTHON_RANGE})")


def check_binary(
    name: str,
    *,
    fix: str,
    required: bool = True,
    which=shutil.which,
) -> Check:
    """An external binary on PATH — the only class of blocker in the contract."""
    found = which(name)
    if found:
        return Check(name, OK, str(found), required=required)
    return Check(
        name,
        MISSING,
        f"not found on PATH (looked for {name!r})",
        required=required,
        fix=fix,
    )


def ffmpeg_fix(platform_name: str) -> str:
    return FFMPEG_FIX.get(platform_name, FFMPEG_FIX_FALLBACK)


def check_extra(extra: str) -> Check:
    """Is the heavy stack for one flow importable here (D4's probe, not a spec)?"""
    if extras.extra_available(extra):
        return Check(f"extra:{extra}", OK, f"installed - {EXTRA_FLOWS[extra]}")
    return Check(
        f"extra:{extra}",
        MISSING,
        f"not installed - {EXTRA_FLOWS[extra]} is unavailable",
        fix=extras.inject_command(extra),
    )


def _is_dangling_symlink(path: Path) -> bool:
    """A link whose target is gone: ``exists()`` is False, but the name is taken.

    ``Path.exists()`` follows the link and answers about the *target*, so a broken
    link is indistinguishable from a free name — the trap that made doctor report
    ``ok`` for a ``yt-cache`` that can never become a directory (m1, [[task:2797]]
    review). ``is_symlink`` is the only view that sees the link itself.

    The ``os.path`` spellings are deliberate: unlike their ``pathlib`` twins they
    swallow ``OSError`` (an unreadable parent is a failed ``lstat``), and this
    helper runs on the diagnos-this-broken-environment path, which reports rather
    than raises (D5, F1).
    """
    return os.path.islink(path) and not os.path.exists(path)


def _nearest_existing(path: Path) -> Path:
    """The first ancestor of ``path`` that exists (``path`` itself when it does).

    Writability of a path that is not there yet is decided by the closest thing
    that is: every flow creates its outputs with ``mkdir(parents=True)``, so the
    question is whether the chain can be built, not whether the leaf exists.

    "Exists" is ``lexists`` on purpose: a dangling symlink occupies the name
    without resolving, and the chain cannot be built *through* it — stopping on
    it is what lets ``check_cache`` name the real reason instead of walking past
    the break to a healthy ancestor and promising the path is creatable.
    """
    probe = path
    while not os.path.lexists(probe) and probe.parent != probe:
        probe = probe.parent
    return probe


def _writable(path: Path) -> bool:
    """Can a new file be created *inside* ``path``? Answered by trying (D8-safe).

    ``os.access(path, W_OK)`` cannot answer this on Windows: it reads the DOS
    read-only attribute, which directories do not carry, so the unwritable
    branch it guarded was dead on the primary platform ([[task:2797]] F4). Only
    the real operation is evidence. The probe is a throwaway file that is
    removed again, so the check still mutates nothing (D8): no cache directory
    is created, and an existing one comes out exactly as it went in.

    The verdict is the *creation*; the cleanup is best effort. If a scanner or
    antivirus still holds the file, failing to remove it must not turn a writable
    directory into a reported wall.
    """
    if not path.is_dir():
        return False
    try:
        handle, name = tempfile.mkstemp(dir=path, prefix=".yt-tools-doctor-", suffix=".tmp")
    except OSError:
        return False
    os.close(handle)
    try:
        os.unlink(name)
    except OSError:
        pass
    return True


def check_cache(root: Path) -> Check:
    """The source-video cache: existence, writability, size — never created here.

    Absent is normal (it appears on the first cached source) and unwritable is a
    ``warn``, not a blocker: transcript-only flows do not need it. A tree that
    cannot be read at all is the very environment doctor exists to explain, so
    every failure here is reported rather than raised ([[task:2797]] F1/F3).
    """
    if root.is_file():
        return Check("cache", WARN, f"{root} is a file, not a directory - cached flows will fail")
    if _is_dangling_symlink(root):
        return Check("cache", WARN, f"{root} is a dangling symlink - cached flows will fail")
    if root.exists():
        if not _writable(root):
            return Check("cache", WARN, f"{root} is not writable - cached flows will fail")
        try:
            entries = cache_list(root)
        except OSError as exc:
            reason = exc.strerror or exc.__class__.__name__
            return Check(
                "cache", WARN, f"{root} cannot be read ({reason}) - cached flows will fail"
            )
        total = sum(e.size_bytes for e in entries)
        videos = f"{len(entries)} video" + ("" if len(entries) == 1 else "s")
        detail = f"{root} - {videos}, {format_size(total)}"
        unreadable = sorted(e.video_id for e in entries if not e.complete)
        if unreadable:
            shown = ", ".join(unreadable[:3])
            if len(unreadable) > 3:
                shown += f", +{len(unreadable) - 3} more"
            return Check(
                "cache",
                WARN,
                f"{detail}; unreadable: {shown} (size is a lower bound)",
            )
        return Check("cache", OK, detail)
    writable_from = _nearest_existing(root)
    if writable_from.is_file():
        return Check("cache", WARN, f"{root} cannot be created - {writable_from} is a file")
    if _is_dangling_symlink(writable_from):
        # Before ``_writable``: probing a dead link for writability answers a
        # question about a path that cannot come into existence at all.
        return Check(
            "cache", WARN, f"{root} cannot be created - {writable_from} is a dangling symlink"
        )
    if not _writable(writable_from):
        return Check(
            "cache", WARN, f"{root} cannot be created - {writable_from} is not writable"
        )
    return Check("cache", OK, f"{root} - not created yet (appears on the first cached source)")


def _read_plugin_json_version(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) else None


def _read_skill_version(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    block = text.split("---\n", 2)[1]
    for line in block.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "version":
            return value.strip() or None
    return None


def check_version(
    *,
    package_version: str,
    pyproject: Path | None = None,
    plugin_json: Path | None = None,
    skill_md: Path | None = None,
) -> Check:
    """Every version source we can *see* against the package's own (D9 consistency).

    An installed wheel carries no source tree, so fewer than two visible sources
    is ``ok`` by construction. The package's own version is labelled
    ``yt_tools.__version__`` rather than "installed metadata": that constant is
    itself a metadata-or-tree fallback, and naming the route it took would be a
    guess. Divergence is a packaging bug (warn): the flows run, the release is wrong.
    """
    sources: dict[str, str] = {"yt_tools.__version__": package_version}
    pyproject_version = version_from_pyproject(pyproject) if pyproject else None
    if pyproject_version:
        sources["pyproject.toml"] = pyproject_version
    plugin_version = _read_plugin_json_version(plugin_json)
    if plugin_version:
        sources["plugin.json"] = plugin_version
    skill_version = _read_skill_version(skill_md)
    if skill_version:
        sources["SKILL.md"] = skill_version

    if len(set(sources.values())) == 1:
        if len(sources) == 1:
            # One source cannot agree with anything — say what is actually true.
            return Check("version", OK, f"{package_version} - only visible source: {next(iter(sources))}")
        return Check("version", OK, f"{package_version} - {', '.join(sources)} agree")

    divergence = ", ".join(f"{name} says {version}" for name, version in sources.items())
    derived = {"plugin.json", "SKILL.md"} & set(sources)
    fix = "python scripts/sync-version.py" if derived else "pipx install --force yt-tools"
    return Check("version", WARN, f"divergence: {divergence}", fix=fix)


# ---- the report --------------------------------------------------------------


def _source_file(root: Path, *parts: str) -> Path | None:
    path = root.joinpath(*parts)
    return path if path.is_file() else None


def collect(
    base: Path | None = None,
    *,
    version_info: tuple[int, ...] | None = None,
    which=shutil.which,
    platform_name: str | None = None,
) -> Report:
    """Run every check against the live environment (read-only)."""
    root = Path(__file__).resolve().parents[1]
    checks = (
        check_python(version_info),
        check_binary(
            "yt-dlp", fix=f"pipx inject {extras.DISTRIBUTION} yt-dlp", which=which
        ),
        check_binary(
            "ffmpeg", fix=ffmpeg_fix(platform_name or sys.platform), which=which
        ),
        *(check_extra(extra) for extra in extras.EXTRA_MODULES),
        check_cache(cache_root(base)),
        check_version(
            package_version=__version__,
            pyproject=_source_file(root, "pyproject.toml"),
            plugin_json=_source_file(root, ".claude-plugin", "plugin.json"),
            skill_md=_source_file(root, "skills", "using-yt-tools", "SKILL.md"),
        ),
    )
    return Report(checks=checks, version=__version__)


def main(
    argv: list[str] | None = None,
    *,
    which=shutil.which,
    version_info: tuple[int, ...] | None = None,
    platform_name: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="yt-tools doctor",
        description=(
            "Preflight the environment: what is present, what is missing, and the "
            "exact command to run next. Installs nothing, changes nothing."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable report (same semantics)"
    )
    parser.add_argument(
        "--base", type=Path, default=None, help="Base dir containing yt-cache/ (default: cwd)"
    )
    args = parser.parse_args(argv)

    # The checks print live data (binary paths, the cache path), not just an ASCII
    # template, so the streams are forced to UTF-8 before anything is written — the
    # convention every other CLI in the suite already follows ([[task:2798]] F2).
    force_utf8_streams()

    report = collect(
        args.base, version_info=version_info, which=which, platform_name=platform_name
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.render())
    return 0 if report.can_proceed else 1


if __name__ == "__main__":
    sys.exit(main())
