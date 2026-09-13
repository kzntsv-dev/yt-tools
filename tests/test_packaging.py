"""Packaging contract — cheap first install ([[requirements:46]] D1–D4, D11, AC1, AC8).

Guards that the heavy stacks live in extras and never in the core
``dependencies``: a plain ``pip install .`` must not pull
opencv/librosa/matplotlib, and every CLI module must stay importable
without them (heavy imports live inside functions, not at module level).

The behavioural halves of AC1/AC8 (a real ``pip install .`` in a clean venv,
``pip list``, and driving the flows) are done by hand — see the task report.
These tests are the fast, network-free half of the same contract, plus the
drift guards that a human check would miss six months from now.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python 3.10
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parents[1]

# D1: the whole core — light flows only.
CORE_DISTS = {"youtube-transcript-api", "yt-dlp"}

# D2/D3: these never belong to the core install.
HEAVY_DISTS = {
    "scenedetect",
    "opencv-python",
    "opencv-python-headless",
    "librosa",
    "matplotlib",
    "rapidocr",
    "onnxruntime",
}

# Import roots that must not appear at module level anywhere in yt_tools/.
# `numpy` is in here on purpose: it is not named by D1, but it reaches a core
# install only through a heavy extra (librosa/matplotlib/opencv), so a
# module-level numpy import breaks the light CLIs in exactly the venv AC1
# describes.
HEAVY_IMPORT_ROOTS = {
    "cv2",
    "scenedetect",
    "librosa",
    "matplotlib",
    "numpy",
    "bpm_detector",
    "rapidocr",
    "onnxruntime",
}

CLI_ENTRY_POINTS = {
    "yt-transcript",
    "yt-meta",
    "yt-comments",
    "yt-search",
    "yt-ocr",
    "yt-frames",
    "yt-watch",
    "yt-listen",
    "yt-tools",
}

HOOK_SCRIPTS = ("ensure-install.sh", "ensure-install.ps1")

# bpm-detector is not on PyPI (404 on both the JSON API and the simple index),
# so it can only arrive as a VCS direct reference — which the *published*
# metadata cannot carry (see the PyPI section below). It is injected on top of
# the `[full]` install instead, best-effort.
BPM_DETECTOR_URL = "git+https://github.com/libraz/bpm-detector@v1.1.0"

# The same two-part shape as HOOK_INSTALL_TARGETS: the inject call itself, and
# the warn-only guard around it (a missing enrichment must never fail the hook).
HOOK_BPM_INJECT: dict[str, tuple[str, ...]] = {
    "ensure-install.sh": (
        r'if\s+!\s+pipx_run\s+inject\s+yt-tools-cli\s+"\$\{BPM_DETECTOR_SPEC\}"',
        r'WARN:.*bpm-detector inject failed',
    ),
    "ensure-install.ps1": (
        r'Invoke-Pipx\s+inject\s+yt-tools-cli\s+\$BpmDetectorSpec',
        r'WARN:.*bpm-detector inject failed',
    ),
}

# The self-heal half: the enrichment lives *outside* the install spec, so a
# version match — the common case on every later session start — skips the
# install branch entirely and nothing would ever put it back. The hook asks the
# CLI itself (`doctor`), whose report carries the same check name and the same
# fix command, so the two cannot drift into disagreeing about what is installed.
HOOK_BPM_SELFHEAL: dict[str, tuple[str, ...]] = {
    "ensure-install.sh": (
        r'command\s+-v\s+yt-tools',
        r'enrichment:bpm-detector',
    ),
    "ensure-install.ps1": (
        r'Get-Command\s+yt-tools',
        r'enrichment:bpm-detector',
    ),
}

# The extras a hook script must pass to its *actual* pipx call. Patterns, not
# substrings: the log lines around these commands also mention `[full]` and
# `[frames,audio]`, so a `in text` check stays green with the command deleted
# (verified by mutation).
HOOK_INSTALL_TARGETS: dict[str, dict[str, tuple[str, ...]]] = {
    "ensure-install.sh": {
        "full": (r'pipx_run\s+"\$\{pipx_args\[@\]\}"\s+"\$\{PLUGIN_ROOT\}\[full\]"',),
        "fallback": (
            r'pipx_run\s+"\$\{pipx_args\[@\]\}"\s+"\$\{PLUGIN_ROOT\}\[frames,audio\]"',
        ),
    },
    "ensure-install.ps1": {
        "full": (
            r'\$fullTarget\s*=\s*"\$PluginRoot\[full\]"',
            r'Invoke-Pipx\s+@pipxArgs\s+\$fullTarget',
        ),
        "fallback": (
            r'\$fallbackTarget\s*=\s*"\$PluginRoot\[frames,audio\]"',
            r'Invoke-Pipx\s+@pipxArgs\s+\$fallbackTarget',
        ),
    },
}


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _dist_name(spec: str) -> str:
    """``scenedetect[opencv]>=0.6.4`` / ``bpm-detector @ git+…`` → ``scenedetect`` / ``bpm-detector``."""
    return canonicalize_name(Requirement(spec).name)


def _names(specs: list[str]) -> set[str]:
    return {_dist_name(spec) for spec in specs}


def _code_lines(text: str) -> str:
    """Drop comment lines so a string test cannot be satisfied by prose.

    ``.sh`` and ``.ps1`` both use ``#``; the hook scripts' comments deliberately
    mention ``[full]`` and ``[frames,audio]``, so a naive ``in text`` check would
    stay green with the executable line deleted.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _module_scope_import_roots(tree: ast.AST) -> set[str]:
    """Import roots reachable *at import time* — module body and its ``if``/``try`` blocks.

    Imports nested in a function, class or lambda body are deferred by
    definition and are the whole point of the design, so they are not collected.
    """
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                found.update(alias.name.split(".")[0] for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                found.add(child.module.split(".")[0])
            visit(child)

    visit(tree)
    return found


# ---- D1: core is light ------------------------------------------------------


def test_core_dependencies_are_only_the_light_stacks():
    deps = _names(_pyproject()["project"]["dependencies"])
    assert deps == CORE_DISTS
    assert not (deps & HEAVY_DISTS), f"heavy stack leaked into core: {sorted(deps & HEAVY_DISTS)}"


# ---- D2: heavy stacks live in their own extras ------------------------------


def test_frames_extra_holds_scenedetect_with_opencv():
    frames = _pyproject()["project"]["optional-dependencies"]["frames"]
    assert "scenedetect" in _names(frames)
    assert any(spec.startswith("scenedetect[") and "opencv" in spec for spec in frames), (
        "the [frames] extra must keep the opencv sub-extra of scenedetect"
    )


def test_audio_extra_holds_librosa_and_matplotlib():
    audio = _names(_pyproject()["project"]["optional-dependencies"]["audio"])
    assert {"librosa", "matplotlib"} <= audio


def test_ocr_extra_is_unchanged():
    ocr = _names(_pyproject()["project"]["optional-dependencies"]["ocr"])
    assert ocr == {"rapidocr", "onnxruntime"}


def test_ocr_extra_pins_rapidocr_below_3_9():
    """`yt_tools/ocr.py` builds the RapidOCR **3.8.x** params schema (task:2831).

    ``params["Det.ocr_version"] = OCRVersion.PPOCRV5`` (+ Cls/Rec) and the
    ``LangRec`` overrides are 3.8.x-shaped. 3.9.x changed the schema and rejects
    the dict at construction — ``error: Invalid OCR configuration`` — which is
    what a clean ``pipx install "yt-tools-cli[ocr]"`` hit: 0.24.0/0.24.1 declared
    ``rapidocr>=3.8``, the resolver picked 3.9.2, and every ``yt-ocr`` run died
    on a machine that had installed exactly what the metadata asked for.

    Same shape as the librosa cap above: an upper bound that encodes a
    compatibility fact, removed only together with the code that depends on it.
    The behavioural half is ``tests/test_ocr_engine.py``, which drives the real
    engine — this one keeps the boundary from drifting back out of the metadata
    where nothing local would notice.
    """
    specs = _pyproject()["project"]["optional-dependencies"]["ocr"]
    req = Requirement(next(spec for spec in specs if _dist_name(spec) == "rapidocr"))

    assert req.specifier.contains("3.8.4"), (
        "the RapidOCR release yt-ocr is written against must stay installable"
    )
    assert not req.specifier.contains("3.9.0"), (
        "rapidocr 3.9 changed the params schema yt_tools/ocr.py builds — lift this "
        "cap only in the same change that adapts the code"
    )


def test_ocr_inject_hint_carries_the_same_bound_as_the_extra():
    """Two install routes, one bound — the `pipx inject` hint has no metadata to inherit.

    ``yt-ocr`` refusing with `pipx inject yt-tools-cli rapidocr onnxruntime` told
    the user to install exactly the pair the extra caps below 3.9 — an inject
    resolves from PyPI directly, so the refusal message would have reproduced
    [[issue:77]] on a machine that followed it to the letter. ``doctor``'s
    ``next_step`` and the README print the same line, so the assertion is on the
    constant all three read from, not on one caller's wording.
    """
    from yt_tools import extras  # noqa: PLC0415 — local import keeps the packaging tests import-light

    specs = dict(
        zip(
            (Requirement(spec).name for spec in extras.EXTRA_PACKAGES["ocr"]),
            extras.EXTRA_PACKAGES["ocr"],
        )
    )
    declared = {
        Requirement(spec).name: spec
        for spec in _pyproject()["project"]["optional-dependencies"]["ocr"]
    }
    assert specs == declared, (
        "the [ocr] extra and the inject hint must name the same requirements — "
        "the hint is what a user pastes when the extra is missing"
    )
    assert '"rapidocr>=3.8,<3.9"' in extras.inject_command("ocr"), (
        "a bounded spec must be quoted: `>` and `<` are redirections in bash, cmd.exe and PowerShell"
    )


def test_ci_installs_the_ocr_extra_so_the_engine_test_actually_runs():
    """A guard test that is skipped in CI is worse than no guard.

    ``tests/test_ocr_engine.py`` skips when rapidocr is absent (the light-core
    jobs install nothing), which is the right shape for a local `pytest` run and
    a silent hole in the pipeline: the suite would stay green while nothing
    exercised the engine — the exact state that let [[issue:77]] ship. So the CI
    install line is part of the contract.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[full,ocr,test]"' in workflow, (
        "the pytest job must install [ocr], or tests/test_ocr_engine.py skips there too"
    )


@pytest.mark.parametrize("extra", ["audio", "full"])
def test_librosa_1x_is_excluded(extra):
    """`bpm-detector` 1.1.0 calls ``librosa.beat.tempo``, removed in librosa 1.0.

    Observed on install (2026-09, fresh venv): ``[full]`` resolved to librosa
    1.0.0 and ``yt-listen`` died inside ``bpm_detector/music_analyzer.py`` with
    ``AttributeError: module 'librosa.beat' has no attribute 'tempo'``; with
    librosa 0.11.0 the same call writes wav + spectrogram + features. Without an
    upper bound the resolver keeps picking 1.x, so the agent default (`[full]`)
    would not actually run every non-OCR flow. Drop the cap once bpm-detector
    supports librosa 1.x.
    """
    specs = _pyproject()["project"]["optional-dependencies"][extra]
    req = Requirement(next(spec for spec in specs if _dist_name(spec) == "librosa"))
    assert req.specifier.contains("0.11.0"), f"[{extra}]: the librosa baseline must stay installable"
    assert not req.specifier.contains("1.0.0"), (
        f"[{extra}]: librosa 1.x breaks bpm-detector (librosa.beat.tempo removed)"
    )


# ---- D3: [full] = core + frames + audio, without [ocr] ----------------------
#
# [test-modify: test_full_extra_covers_core_frames_audio_plus_bpm_detector: was
#  `expected = CORE_DISTS | _names(extras["frames"]) | _names(extras["audio"]) |
#   {"bpm-detector"}`; is `expected = CORE_DISTS | _names(extras["frames"]) |
#   _names(extras["audio"])` and the test is renamed to
#   `test_full_extra_covers_core_frames_and_audio`, with the exclusion asserted
#   separately below; reason: PyPI rejected the v0.23.0 upload with 400
#   "Can't have direct dependency: bpm-detector @ git+…" — a direct reference
#   cannot be published, so bpm-detector moved out of the metadata and into the
#   plugin hook's best-effort inject (requirements:46 D3 amended)]


def test_full_extra_covers_core_frames_and_audio():
    project = _pyproject()["project"]
    extras = project["optional-dependencies"]
    # PEP 621: extras are additive to `dependencies`, so `pip install yt-tools[full]`
    # installs core + [full]. Assert the effective installed set exactly — this
    # also catches anything unexpected leaking in (e.g. the OCR model).
    effective_full = _names(project["dependencies"]) | _names(extras["full"])

    expected = CORE_DISTS | _names(extras["frames"]) | _names(extras["audio"])
    assert effective_full == expected


def test_full_extra_excludes_bpm_detector_because_it_cannot_be_published():
    """The enrichment is injected by the hook, never declared in the metadata.

    Not a style preference: `Requires-Dist` with a direct reference is rejected
    by PyPI on upload (HTTP 400), so any `[full]` that carries it is
    unpublishable — and nothing local catches that before the tag.
    """
    full = _pyproject()["project"]["optional-dependencies"]["full"]
    assert not ({"bpm-detector"} & _names(full)), (
        "D3 (amended): bpm-detector is a VCS dep and cannot be published — inject it from the hook"
    )


def test_full_repeats_the_extra_pins_verbatim():
    """`[full]` cannot reference `[frames]`/`[audio]` (PEP 621 has no self-extras).

    So it repeats their requirements — and a duplicated pin drifts silently the
    next time someone bumps it in one place. Same dist, same specifier string,
    or this fails.
    """
    extras = _pyproject()["project"]["optional-dependencies"]
    full = {_dist_name(spec): spec for spec in extras["full"]}

    for extra in ("frames", "audio"):
        for spec in extras[extra]:
            dist = _dist_name(spec)
            assert dist in full, f"[full] is missing {dist} from [{extra}]"
            assert full[dist] == spec, (
                f"[full] pins {dist!r} as {full[dist]!r} while [{extra}] pins {spec!r} — "
                "keep the duplicated requirement in sync"
            )


def test_full_extra_excludes_the_ocr_model():
    project = _pyproject()["project"]
    full = _names(project["optional-dependencies"]["full"])
    assert not (full & {"rapidocr", "onnxruntime"}), "D3: the OCR model is a lazy, explicit install"


# ---- PyPI: the published metadata must carry no direct references ----------
#
# PyPI rejects PEP 508 direct references in Requires-Dist with an HTTP 400.
# Observed live on the v0.23.0 upload (2026-09-13) — the release died at the
# index with:
#
#   400 Bad Request — Can't have direct dependency:
#   bpm-detector @ git+https://github.com/libraz/bpm-detector@v1.1.0 ; extra == "full"
#
# A direct ref builds fine and `twine check` is happy, so the only other place
# this surfaces is the upload — on a tag, where it costs a version number.
# Source-level guard it is.

DIRECT_REF_RE = re.compile(
    r"@\s*(?:git\+|hg\+|svn\+|bzr\+|https?://|file:|ssh://)", re.IGNORECASE
)

#: Path segments that exist only in the private dev repo. The artifact-level
#: gate (``scripts/check-metadata.py``) owns the list for built archives; this
#: is the source-level half of the same contract.
META_ARTIFACT_PATHS = {
    "AGENTS.md",
    "CLAUDE.md",
    ".mappa",
    ".mappa-manifest.json",
    ".pi",
    ".wiki",
    ".tasks",
}


def _requirement_groups() -> dict[str, list[str]]:
    project = _pyproject()["project"]
    groups = {"dependencies": list(project["dependencies"])}
    for extra, specs in project["optional-dependencies"].items():
        groups[f"optional-dependencies.{extra}"] = list(specs)
    return groups


def test_sdist_build_excludes_dev_repo_meta():
    """The public sdist must not carry the dev repo's meta — in any tree.

    ``python -m build`` in the *dev* tree picks up everything git does not
    ignore, and dev's ``.gitignore`` deliberately keeps ``AGENTS.md``,
    ``.mappa/`` and ``.pi/`` (its negation rules mirror pub's backstop). The
    curated copy drops them, so this only bites when a build runs in dev — which
    is precisely how a canon snapshot would get published by accident.
    ``scripts/check-metadata.py`` is the artifact-level gate (CI and the release
    job); this is the fast guard that the exclude list stays real.
    """
    hatch = _pyproject().get("tool", {}).get("hatch", {})
    sdist = hatch.get("build", {}).get("targets", {}).get("sdist", {})
    patterns = {p.strip().strip("/").removesuffix("/**") for p in sdist.get("exclude", [])}
    missing = META_ARTIFACT_PATHS - patterns
    assert not missing, (
        f"the sdist target must exclude {sorted(missing)} — dev-repo meta in a "
        "published sdist is a leak; see scripts/check-metadata.py"
    )


def test_published_metadata_carries_no_direct_references():
    offenders = {
        group: [spec for spec in specs if DIRECT_REF_RE.search(spec)]
        for group, specs in _requirement_groups().items()
    }
    offenders = {group: specs for group, specs in offenders.items() if specs}
    assert not offenders, (
        f"PyPI rejects direct references in Requires-Dist (HTTP 400 on upload): {offenders}. "
        "Install such a dependency outside the metadata (hook inject / a README step)."
    )


def test_hatch_direct_reference_escape_hatch_is_closed():
    """`allow-direct-references` is what let the rejected metadata build at all.

    It is a build-time permission, not a publish-time one: with it on, hatchling
    happily writes `bpm-detector @ git+…` into METADATA and the failure is
    deferred to the upload. Removing the last direct ref makes it dead weight;
    keeping it off means the next one fails at `python -m build`, locally.
    """
    hatch = _pyproject().get("tool", {}).get("hatch", {})
    assert not hatch.get("metadata", {}).get("allow-direct-references", False), (
        "tool.hatch.metadata.allow-direct-references is set — it lets a direct "
        "reference build and defers the failure to the PyPI upload"
    )


# ---- D11: no breaking CLI change -------------------------------------------


def test_cli_entry_points_unchanged():
    assert set(_pyproject()["project"]["scripts"]) == CLI_ENTRY_POINTS


# ---- AC1: light CLIs import cleanly, without the heavy stacks ---------------


@pytest.mark.parametrize(
    "module_path", sorted((ROOT / "yt_tools").glob("*.py")), ids=lambda p: p.name
)
def test_no_module_level_heavy_imports(module_path):
    """Source-level guard — the one that holds in *any* environment.

    A module-level heavy import breaks a core-only install whether or not the
    stack happens to be present in the test interpreter, so this check does not
    depend on what is installed.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    leaked = _module_scope_import_roots(tree) & HEAVY_IMPORT_ROOTS
    assert not leaked, (
        f"{module_path.name}: heavy stack imported at module level: {sorted(leaked)} — "
        "move it inside the function that needs it"
    )


# Import every CLI module top-level, then report which heavy stacks got pulled
# in as a side effect of those imports alone. Note both worlds are meaningful:
# with the heavy stacks absent an eager import raises (rc != 0), with them
# present it shows up in the list — neither passes silently.
_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "MODULES = (\n"
    "    'yt_tools.transcript', 'yt_tools.meta', 'yt_tools.comments', 'yt_tools.search',\n"
    "    'yt_tools.cli', 'yt_tools.frames', 'yt_tools.watch', 'yt_tools.listen',\n"
    "    'yt_tools.ocr', 'yt_tools.core',\n"
    ")\n"
    "for name in MODULES:\n"
    "    importlib.import_module(name)\n"
    "HEAVY = sorted(m for m in (\n"
    "    'cv2', 'scenedetect', 'librosa', 'matplotlib', 'numpy',\n"
    "    'bpm_detector', 'rapidocr', 'onnxruntime',\n"
    ") if m in sys.modules)\n"
    "import importlib.util as u\n"
    "print(json.dumps({'pulled': HEAVY, 'installed': sorted(\n"
    "    m for m in ('cv2', 'scenedetect', 'librosa', 'matplotlib', 'numpy',\n"
    "                'bpm_detector', 'rapidocr', 'onnxruntime') if u.find_spec(m))}))\n"
)


def test_importing_clis_does_not_pull_heavy_stacks():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"CLI import failed (eager heavy import?):\n{proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["pulled"] == [], (
        f"heavy stacks imported eagerly: {report['pulled']} "
        f"(installed here: {report['installed']})"
    )


# ---- AC8: the plugin hook keeps installing [full] ---------------------------


@pytest.mark.parametrize("script_name", HOOK_SCRIPTS)
def test_hook_install_target_is_full(script_name):
    """AC8: the plugin default stays `[full]`."""
    code = _code_lines((ROOT / "scripts" / script_name).read_text(encoding="utf-8"))
    for pattern in HOOK_INSTALL_TARGETS[script_name]["full"]:
        assert re.search(pattern, code), (
            f"{script_name}: no executable `[full]` install target matches {pattern!r}"
        )


@pytest.mark.parametrize("script_name", HOOK_SCRIPTS)
def test_hook_fallback_keeps_frames_and_audio(script_name):
    """If the bpm-detector VCS fetch fails, the executable fallback must not drop B and C.

    The old fallback was a bare core install, which after the slimming would
    lose scenedetect/librosa/matplotlib — the agent default must survive a
    blocked VCS fetch with every flow except `[ocr]` still working (AC8).
    """
    code = _code_lines((ROOT / "scripts" / script_name).read_text(encoding="utf-8"))
    for pattern in HOOK_INSTALL_TARGETS[script_name]["fallback"]:
        assert re.search(pattern, code), (
            f"{script_name}: the executable fallback target must be [frames,audio], not bare core "
            f"(no match for {pattern!r})"
        )


@pytest.mark.parametrize("script_name", HOOK_SCRIPTS)
def test_hook_injects_bpm_detector_best_effort(script_name):
    """bpm-detector cannot ride the published `[full]` extra, so the hook adds it.

    The enrichment (chord progression, structure, refined BPM/key) is worth a
    VCS fetch on the agent path, but it is not worth a failed install: a blocked
    fetch (corporate proxy) must leave the librosa-only path, which every flow
    already tolerates. Both halves are asserted — the inject call, and the
    WARN-not-exit guard around it.
    """
    code = _code_lines((ROOT / "scripts" / script_name).read_text(encoding="utf-8"))
    for pattern in HOOK_BPM_INJECT[script_name]:
        assert re.search(pattern, code), (
            f"{script_name}: bpm-detector inject missing or not warn-only (no match for {pattern!r})"
        )


@pytest.mark.parametrize("script_name", HOOK_SCRIPTS)
def test_hook_tops_up_bpm_detector_when_the_version_already_matches(script_name):
    """The gap the inject alone leaves open.

    `bpm-detector` cannot be part of `[full]` (it is not on PyPI and PyPI rejects
    direct references in metadata), so nothing in the install spec guarantees it.
    The inject above only runs after a reinstall — which happens once, on a
    version change. Every later session start takes the "version matches" path
    and would leave a machine that once lost the VCS fetch (corporate proxy) on
    the librosa-only path forever, quietly. So the hook asks the CLI.
    """
    code = _code_lines((ROOT / "scripts" / script_name).read_text(encoding="utf-8"))
    for pattern in HOOK_BPM_SELFHEAL[script_name]:
        assert re.search(pattern, code), (
            f"{script_name}: no self-heal probe for the enrichment (no match for {pattern!r}); "
            "a version match skips the install branch and the inject with it"
        )


@pytest.mark.parametrize("script_name", HOOK_SCRIPTS)
def test_hook_pins_bpm_detector_to_the_url_requirement_46_records(script_name):
    """The spec lives in the hook now, so the URL is asserted there — verbatim."""
    code = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
    assert BPM_DETECTOR_URL in code, (
        f"{script_name}: expected the bpm-detector VCS spec to carry {BPM_DETECTOR_URL!r}"
    )


def test_hook_shell_script_parses():
    """Content guards do not see syntax.

    A stray `fi` (from a restructure that moved a block out of an `if`) passed
    every regex above and every unit test — the packaging suite asserts strings,
    not grammar. It surfaced only when the hook was run by hand. `bash -n` is the
    cheap check that sees it, and it costs milliseconds on every push.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - Windows without Git Bash
        pytest.skip("no bash on PATH")
    proc = subprocess.run(
        [bash, "-n", str(ROOT / "scripts" / "ensure-install.sh")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"ensure-install.sh does not parse:\n{proc.stderr}"


def test_hook_powershell_script_parses():
    """Same guard for the Windows hook (skipped where no PowerShell exists)."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:  # pragma: no cover - Linux/macOS CI
        pytest.skip("no PowerShell on PATH")
    script = str(ROOT / "scripts" / "ensure-install.ps1")
    parse = (
        f"$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$errors); "
        f"if ($errors.Count) {{ $errors | ForEach-Object {{ Write-Error $_.Message }}; exit 1 }}"
    )
    proc = subprocess.run(
        [shell, "-NoProfile", "-Command", parse], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"ensure-install.ps1 does not parse:\n{proc.stderr}"


# ---- documentation ----------------------------------------------------------


def test_readme_documents_bpm_detector_outside_the_metadata():
    """The README table is where a PyPI user learns why the enrichment is extra work.

    `[full]` no longer pulls bpm-detector (it cannot), so a README that still
    advertises it as part of `[full]` promises a capability the install does not
    deliver.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "bpm-detector" in readme, "README must still mention the optional enrichment"
    assert "pipx inject yt-tools-cli" in readme, (
        "README must show how to add bpm-detector on top of an install"
    )
    full_row = next(
        (line for line in readme.splitlines() if line.startswith("| `[full]`")), ""
    )
    assert "bpm-detector" not in full_row, (
        "the `[full]` table row must not advertise bpm-detector: the extra cannot carry it"
    )


def test_readme_documents_every_extra():
    """Presence check, not a correctness check: extra *names* come from pyproject,
    so renaming an extra without touching the README fails here.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    extras = _pyproject()["project"]["optional-dependencies"]
    for extra in sorted(name for name in extras if name != "test"):
        assert f"[{extra}]" in readme, f"README does not document the [{extra}] install flow"
