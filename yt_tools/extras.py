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

# extra → distribution names for the ``pipx inject`` hint.
EXTRA_PACKAGES: dict[str, tuple[str, ...]] = {
    "frames": ("scenedetect", "opencv-python"),
    "audio": ("librosa", "matplotlib"),
    "ocr": ("rapidocr", "onnxruntime"),
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
    packages = " ".join(EXTRA_PACKAGES[extra])
    return f"pipx inject {DISTRIBUTION} {packages}"


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
