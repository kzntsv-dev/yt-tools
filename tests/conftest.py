"""Shared fixtures for the extras-gated suite ([[task:2842]], [[issue:75]]).

The suite ships a light core and heavy extras ([[requirements:46]] D1/D2), and
CI runs it on two legs: core-only and ``[full,ocr]``. A test that needs
``[frames]``/``[audio]``/``[ocr]`` must therefore **skip with a named reason**
when the extra is absent — a missing extra is a normal install (AC1), not a
broken suite. That is the class ``issue:75`` belonged to: a bare
``import librosa`` in a test body reddened a light install, and the real
regression drowned in the noise.

The three ``*_stack`` fixtures are the **single declaration** of that
dependency:

* they probe the extra the way the CLI does (:mod:`yt_tools.extras`) and skip
  with the extra's name plus the exact command that installs it;
* :func:`pytest_collection_modifyitems` turns the fixture *request* into the
  ``requires_frames`` / ``requires_audio`` / ``requires_ocr`` marker, so
  ``pytest -m requires_<extra>`` selects the gated set and
  :func:`pytest_sessionfinish` can fail the leg when a gated test ran without
  its extra, when one stopped skipping with the extra installed, or when the
  gated set quietly shrank. Marker and gate cannot drift apart, because they
  are the same declaration.

Run a full-suite leg with ``--extras-gate-floors`` to enforce the per-extra
counts as well (CI does; a leg that runs two files does not).

References: [[task:2842]] (the gate), [[issue:75]] (the failure class),
[[requirements:46]] AC1 (a missing extra is a normal install, not a red suite).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tests.extras_gate import GATED_MINIMUM, outcome_from_report, session_reports, violations
from yt_tools import extras

#: fixture name → the extra it gates. The fixture is what tests ask for; the
#: marker is derived from it (see the hook below), never written by hand.
EXTRA_FIXTURES: dict[str, str] = {
    "frames_stack": "frames",
    "audio_stack": "audio",
    "ocr_stack": "ocr",
}

#: extra → the marker the gate counts that extra's tests by. Plain names, not
#: ``requires_extra("frames")``: marker *expressions* do not match marker
#: arguments, so ``-m requires_frames`` is what selects a gated set by hand.
EXTRA_MARKERS: dict[str, str] = {
    "frames": "requires_frames",
    "audio": "requires_audio",
    "ocr": "requires_ocr",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("extras gate").addoption(
        "--extras-gate-floors",
        action="store_true",
        default=False,
        help=(
            "enforce that the gated set did not shrink (tests/extras_gate.py GATED_MINIMUM) "
            "and treat a gated test with no report as a violation; a claim about the whole "
            "suite, so do not combine it with -k/-m/-x"
        ),
    )
    parser.getgroup("extras gate").addoption(
        "--extras-gate-expect",
        default="",
        metavar="EXTRAS",
        help=(
            "comma-separated extras this leg *installs on purpose* (e.g. 'frames' or "
            "'frames,audio,ocr'): the leg fails when one of them is not importable. Without "
            "this claim an install that silently dropped a dependency is indistinguishable "
            "from a leg that never had the extra — every gated test skips and the job is green"
        ),
    )


#: nodeid → extra, filled at collection from the fixture each test requested.
_GATED: dict[str, str] = {}

#: nodeid → (outcome, reason), filled from the test's reports.
_OUTCOMES: dict[str, tuple[str, str]] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start every session from a clean slate.

    The module outlives a session when pytest is driven in-process (IDE runners,
    ``pytester``), and state leaking across two runs would judge one suite by
    the other's reports.
    """
    _GATED.clear()
    _OUTCOMES.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test that asked for an extras fixture, and remember why.

    ``tryfirst`` so the markers exist before anything filters on them —
    ``-m requires_frames`` (and ``-m "not requires_frames"``) has to see the
    same set the gate counts, whichever pytest version orders these hooks.
    """
    for item in items:
        for fixture_name, extra in EXTRA_FIXTURES.items():
            if fixture_name in item.fixturenames:
                item.add_marker(getattr(pytest.mark, EXTRA_MARKERS[extra]))
                _GATED[item.nodeid] = extra


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record gated tests only — the rest of the suite is pytest's own business."""
    if report.nodeid in _GATED:
        verdict = outcome_from_report(report)
        if verdict is not None:
            _OUTCOMES[report.nodeid] = verdict


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when a leg lied about its extras ([[task:2842]]).

    The verdict is not a test result: no test is red, the *leg* is wrong. So it
    is raised here, at session level, where it cannot be mistaken for a flaky
    test and cannot be silenced by marking a test xfail.
    """
    # A collection is not a run: nothing was executed, so there is nothing to judge —
    # and neither is a plan (`--setup-plan`/`--setup-only`).
    options = session.config.option
    if options.collectonly or getattr(options, "setuponly", False) or getattr(options, "setup_plan", False):
        return

    strict = session.config.getoption("--extras-gate-floors")
    if not _GATED and not strict:
        # Nothing extras-gated ran here (a single non-extras file, say) — stay quiet.
        return

    installed = {extra for extra in set(_GATED.values()) if extras.extra_available(extra)}
    expected_names = [
        name.strip()
        for name in (session.config.getoption("--extras-gate-expect") or "").split(",")
        if name.strip()
    ]
    unknown = [name for name in expected_names if name not in extras.EXTRA_MODULES]
    expected = {
        name: extras.missing_modules(name) for name in expected_names if name not in unknown
    }
    reports = session_reports(
        _GATED,
        _OUTCOMES,
        (item.nodeid for item in session.items),
        strict=strict,
    )
    problems = [
        f"--extras-gate-expect names an unknown extra: {name!r} "
        f"(known: {', '.join(sorted(extras.EXTRA_MODULES))})"
        for name in unknown
    ] + violations(
        reports, installed, expected=expected, minimums=GATED_MINIMUM if strict else {}
    )

    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    counts = " ".join(
        f"{extra}={sum(1 for report in reports if report.extra == extra)}"
        for extra in sorted(set(_GATED.values()))
    )
    if terminal is not None:
        terminal.write_sep("=", "extras gate")
        terminal.write_line(f"gated tests: {counts or 'none'}; extras present: {sorted(installed) or 'none'}")
        if expected_names:
            terminal.write_line(
                "this leg installs: "
                + ", ".join(
                    f"[{name}]" if not expected.get(name) else f"[{name}] MISSING"
                    for name in expected_names
                    if name not in unknown
                )
            )

    if not problems:
        if terminal is not None:
            terminal.write_line("every gated test skipped with a reason, or ran with its extra")
        return

    session.exitstatus = 1
    if terminal is not None:
        terminal.write_line(f"{len(problems)} violation(s):")
        for problem in problems:
            terminal.write_line(f"  - {problem}")


def skip_reason(extra: str, missing: Sequence[str]) -> str:
    """What the missing extra is called, and the one command that installs it.

    The gate only requires that a skip *has* a reason (``violations()`` in
    ``tests/extras_gate.py``); this is the wording both share, so "named" means
    the same thing to a human reading the log and to the rule checking it.
    """
    return (
        f"the [{extra}] extra is not installed ({', '.join(missing)} not importable) - "
        f"pip install 'yt-tools-cli[{extra}]'"
    )


def _require_extra(extra: str) -> None:
    """Skip — never fail — when ``extra`` is absent, the way the CLI probes it.

    Probing by import, not by looking for a spec, keeps the test's notion of
    "installed" identical to :func:`yt_tools.extras.require_extra`, which is
    what the product refuses on: an installed-but-broken native stack has a
    spec and still explodes on import.
    """
    missing = extras.missing_modules(extra)
    if missing:
        pytest.skip(skip_reason(extra, missing))


@pytest.fixture(scope="module")
def frames_stack() -> None:
    """``[frames]`` (scenedetect + cv2/opencv) present, or a skip.

    Everything that drives ``yt-frames``/``yt-watch`` past their first
    ``require_extra`` needs it, and so does any test that renders a real frame
    file for the dedup metric: ``cv2`` arrives with this extra and with no
    other.
    """
    _require_extra("frames")


@pytest.fixture(scope="module")
def audio_stack() -> tuple[object, object]:
    """``(numpy, librosa)`` — the ``[audio]`` extra — or a skip ([[task:2840]]).

    The pipeline tests want both; the key-estimation tests use numpy arrays as
    their input type. Deliberately **not** a module-level ``importorskip``:
    the parsers, formatters and markdown tests in the same modules are pure and
    must keep running without the heavy stack — silently switching them off
    would trade a visible failure for an invisible loss of coverage.
    """
    _require_extra("audio")
    import librosa
    import numpy as np

    return np, librosa


@pytest.fixture(scope="module")
def ocr_stack() -> None:
    """``[ocr]`` (rapidocr + onnxruntime) present, or a skip.

    ``tests/test_ocr_engine.py`` also declares the marker at module level
    because it skips through ``pytest.importorskip`` before collection (the
    engine must not be imported at all when the extra is absent); this fixture
    is the way for any *other* test to ask for the same gate.
    """
    _require_extra("ocr")
