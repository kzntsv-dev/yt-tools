"""Missing-extra refusal — one wording for every CLI ([[requirements:46]] D4).

The suite ships a light core and heavy extras (``[frames]`` / ``[audio]`` /
``[ocr]``). A flow whose extra is absent must fail *named*: the reason, the
exact install command, and a non-zero exit code — never an ``ImportError``
raised from the depths of a third-party stack, and never a silent quality drop.

One formatter, so ``yt-frames``, ``yt-watch``, ``yt-listen`` and ``yt-ocr``
say the same thing in the same shape. ``doctor`` (a later task in the same
plan) reports availability separately; this module is only about refusing.

Availability is probed by *importing*, not by looking for a spec: an installed
distribution whose native library is missing (ABI drift, half-removed CUDA
build, truncated download) has a spec and still explodes on import — which is
precisely the deep ``ImportError`` this module exists to replace.
"""

from __future__ import annotations

import sys
from importlib import import_module

#: The **distribution** name — what `pip`/`pipx` resolve and what PyPI accepts.
#: Not the project name: `yt-tools` is unregisterable on PyPI (its similarity
#: folding collides with the existing `yttools`, [[wiki:3589]]), while the
#: project, repo, plugin and every console command stay `yt-tools`.
DISTRIBUTION = "yt-tools-cli"

# extra → modules the flow imports at run time. Import names, which are not
# distribution names: ``cv2`` comes from ``opencv-python``.
EXTRA_MODULES: dict[str, tuple[str, ...]] = {
    "frames": ("scenedetect", "cv2"),
    "audio": ("librosa", "matplotlib"),
    "ocr": ("rapidocr", "onnxruntime"),
}

# extra → install specs for the ``pipx inject`` hint. These are what the user
# actually pastes, and an inject resolves them *outside* the extra's metadata —
# so a bound that matters has to be repeated here or the hint contradicts the
# extra it is advertising: `pipx inject yt-tools-cli rapidocr` used to resolve
# 3.9.x while `[ocr]` said <3.9, i.e. the refusal message itself installed the
# broken pair ([[task:2831]]). tests/test_packaging.py pins the OCR entries to
# the requirement strings in pyproject.toml so the two routes cannot drift.
EXTRA_PACKAGES: dict[str, tuple[str, ...]] = {
    "frames": ("scenedetect", "opencv-python"),
    "audio": ("librosa", "matplotlib"),
    "ocr": ("rapidocr>=3.8,<3.9", "onnxruntime>=1.18"),
}

#: The enrichment that cannot be a package extra. ``bpm-detector`` gives
#: ``yt-listen`` chord progression, structural segments and a refined BPM/key,
#: but it is not on PyPI — so it can only be a PEP 508 direct reference, and PyPI
#: rejects those in ``Requires-Dist`` (release 0.23.0 died on exactly that,
#: [[task:2824]]). It is therefore installed with ``pipx inject``: by the plugin
#: hook, or by hand from the README. ``doctor`` names it for the same reason it
#: names a missing extra — a silent quality drop is worse than a named one (D6).
BPM_DETECTOR_MODULE = "bpm_detector"
BPM_DETECTOR_SPEC = "bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0"
BPM_DETECTOR_FIX = f'pipx inject {DISTRIBUTION} "{BPM_DETECTOR_SPEC}"'

_MISSING = object()


class MissingExtra(RuntimeError):
    """A flow needs an extra that is not installed.

    ``str(exc)`` is the user-facing refusal — callers print it, they do not
    re-word it. Subclasses ``RuntimeError`` so the existing CLI entry points
    (which already catch ``RuntimeError``) report it without a traceback.
    """


def format_missing_extra(feature: str, extra: str) -> str:
    """Refusal text: what needs what, and the exact command that fixes it."""
    _check_extra(extra)
    return f"{feature} requires the [{extra}] extra:\n{_install_lines(extra)}"


def _check_extra(extra: str) -> None:
    if extra not in EXTRA_PACKAGES:
        raise ValueError(
            f"unknown extra: {extra!r} (known: {', '.join(sorted(EXTRA_PACKAGES))})"
        )


def _install_lines(extra: str) -> str:
    return (
        f"  {inject_command(extra)}\n"
        f"  # or\n"
        f"  pip install '{DISTRIBUTION}[{extra}]'"
    )


def inject_command(extra: str) -> str:
    """The one exact ``pipx`` command that adds ``extra`` to an existing venv.

    Public because ``doctor`` names the same command in its ``next_step`` — the
    refusal and the recommendation must not drift apart.
    """
    _check_extra(extra)
    return inject_command_for([extra])


def inject_command_for(extra_names: list[str] | tuple[str, ...]) -> str:
    """One command adding several extras — what ``doctor``'s ``next_step`` prints.

    Every spec is double-quoted: an injected spec can carry version bounds
    (``rapidocr>=3.8,<3.9``), and an unquoted ``>``/``<`` is a redirection in
    bash, cmd.exe and PowerShell alike — the printed command has to be
    pasteable, since being pasteable is the whole point of it.
    """
    specs = [spec for name in extra_names for spec in _specs(name)]
    return f"pipx inject {DISTRIBUTION} " + " ".join(f'"{spec}"' for spec in specs)


def _specs(extra: str) -> tuple[str, ...]:
    _check_extra(extra)
    return EXTRA_PACKAGES[extra]


def module_missing(name: str) -> bool:
    """True when ``name`` cannot be imported here.

    A ``None`` entry in ``sys.modules`` counts as missing — that is how a module
    is poisoned in tests, and the import would fail on it anyway.
    """
    if sys.modules.get(name, _MISSING) is None:
        return True
    try:
        import_module(name)
    except Exception:  # noqa: BLE001 — any import-time failure means "not usable"
        return True
    return False


def missing_modules(extra: str, modules: tuple[str, ...] | None = None) -> list[str]:
    """The subset of ``modules`` (default: the extra's own) that is not importable."""
    _check_extra(extra)
    return [m for m in (EXTRA_MODULES[extra] if modules is None else modules) if module_missing(m)]


def extra_available(extra: str, modules: tuple[str, ...] | None = None) -> bool:
    return not missing_modules(extra, modules)


def require_extra(feature: str, extra: str, modules: tuple[str, ...] | None = None) -> None:
    """Refuse with the shared message when the extra is not installed."""
    if not extra_available(extra, modules):
        raise MissingExtra(format_missing_extra(feature, extra))


def warn_missing_extra(feature: str, extra: str, modules: tuple[str, ...] | None = None) -> bool:
    """Degradable case: the flow still runs, but the quality drop is announced.

    Returns True when the extra is missing, i.e. when the caller should skip the
    optional step. The wording is *not* the fatal one — nothing is required here,
    a feature is lost — but the command is the same, and the drop is announced
    rather than silent: an unannounced drop is an answer the agent cannot judge (D6).
    """
    if extra_available(extra, modules):
        return False
    print(
        f"note: {feature} skipped - the [{extra}] extra is not installed "
        f"(continuing without it).\nInstall it to get it back:\n{_install_lines(extra)}",
        file=sys.stderr,
    )
    return True
