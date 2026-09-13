"""The extras gate's verdict — one rule per way a CI leg can lie ([[task:2842]]).

The suite's contract with CI is narrow: a test that needs an extra **skips,
naming the extra**, when it is absent, and **runs** when it is present.
Everything the gate catches follows from negating those two halves, plus the
failure that started this ([[issue:75]]): a gated test that ran without its
extra and took a light install down with it — or, quieter and worse, ran and
passed while the extra's absence went unnoticed.

No I/O here: the wiring (pytest hooks, "which extras are installed") lives in
``tests/conftest.py``. This module is the pure half, so the rules are pinned by
tests rather than by inspecting a red CI run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: Outcomes as the hooks report them. ``MISSING`` is the gate's own word for a
#: gated test that produced no report at all (deselected, or dropped by a
#: collection error in a sibling) — collected is not the same as seen.
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
MISSING = "missing"

#: extra → how many gated tests the **full** suite must contain. Set to the
#: counts as they stand: the gate may grow freely (this is a lower bound), but it
#: must never *shrink* without a deliberate edit here — a stripped `*_stack`
#: request removes the marker with the skip, and only this number notices. Lower
#: one only in a commit that removes those tests, and say why.
#: Enforced only when CI asks for it (subset legs — the rapidocr matrix runs two
#: files — are not judged on the whole suite's counts).
GATED_MINIMUM: dict[str, int] = {
    "frames": 53,
    "audio": 11,
    "ocr": 8,
}


@dataclass(frozen=True)
class Report:
    """One gated test, as the session saw it."""

    nodeid: str
    extra: str
    outcome: str
    reason: str = ""


def violations(
    reports: Sequence[Report],
    installed: Iterable[str],
    *,
    expected: Mapping[str, Sequence[str]] | None = None,
    minimums: Mapping[str, int] | None = None,
) -> list[str]:
    """Everything wrong with this leg, as printable lines. Empty list = green.

    ``installed`` is what the environment actually has (``yt_tools.extras``
    probes it by import, the way the CLI does). ``expected`` is the *other*
    direction — the leg's own claim about what it installed (extra → the modules
    that are not importable, i.e. empty when the install delivered it). Without
    that claim a leg whose extra silently failed to install looks exactly like a
    leg that never had the extra: every gated test skips "legitimately" and the
    job goes green on nothing ([[issue:77]] was that shape). ``minimums``
    defaults to :data:`GATED_MINIMUM`; pass ``{}`` for a leg that runs only part
    of the suite.
    """
    available = set(installed)
    floors = GATED_MINIMUM if minimums is None else minimums

    problems: list[str] = []
    for extra, missing in sorted((expected or {}).items()):
        if missing:
            problems.append(
                f"[{extra}]: this leg installs it on purpose, but {', '.join(missing)} "
                f"not importable — the install did not deliver the extra "
                f"(pip install 'yt-tools-cli[{extra}]')"
            )

    for report in reports:
        if report.extra in available:
            problems.extend(_installed_problems(report))
        else:
            problems.extend(_absent_problems(report))

    for extra, floor in floors.items():
        seen = sum(1 for report in reports if report.extra == extra)
        if seen < floor:
            problems.append(
                f"[{extra}]: only {seen} gated test(s) reported, expected at least {floor} — "
                "the gated set shrank (requesting the *_stack fixture is what marks a test; "
                "lower GATED_MINIMUM only in a commit that removes those tests)"
            )
    return problems


def session_reports(
    gated: Mapping[str, str],
    outcomes: Mapping[str, tuple[str, str]],
    selected: Iterable[str],
    *,
    strict: bool,
) -> list[Report]:
    """The reports this run is allowed to judge.

    ``gated`` is nodeid → extra as collection saw it, ``outcomes`` what the run
    recorded, ``selected`` the items this session actually selected. Tests the
    run did not select are ignored — a subset run is not a lying leg. A selected
    gated test with no report at all is reported as :data:`MISSING` only in
    ``strict`` mode (the whole-suite CI legs): everywhere else it means the run
    was truncated (`-x`, an interrupt), not that the gate lost a test.
    """
    chosen = set(selected)
    reports = []
    for nodeid, extra in sorted(gated.items()):
        if nodeid not in chosen:
            continue
        outcome, reason = outcomes.get(nodeid, (MISSING, ""))
        if outcome == MISSING and not strict:
            continue
        reports.append(Report(nodeid=nodeid, extra=extra, outcome=outcome, reason=reason))
    return reports


def outcome_from_report(report: object) -> tuple[str, str] | None:
    """Translate one pytest report into ``(outcome, reason)``, or None if it carries no verdict.

    pytest reports three phases per test; only one of them decides its fate.
    Skipping happens at setup, passing and failing at call, and a setup/teardown
    explosion is an ``error`` rather than a plain ``failed`` — the report should
    keep that difference, because an error raised *by the gate fixture* is the
    gate itself misbehaving. Setup/teardown *passes* carry nothing.
    """
    if getattr(report, "skipped", False):
        return SKIPPED, _reason(report)
    if getattr(report, "failed", False):
        when = getattr(report, "when", "call")
        return ("error" if when in ("setup", "teardown") else FAILED), str(getattr(report, "longrepr", ""))
    if getattr(report, "when", None) == "call" and getattr(report, "passed", False):
        return PASSED, ""
    return None


def _reason(report: object) -> str:
    """A skip's reason — pytest hands it over as ``(path, lineno, reason)``."""
    longrepr = getattr(report, "longrepr", "")
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def _installed_problems(report: Report) -> list[str]:
    """The extra is here, so the test has no excuse not to run."""
    if report.outcome == SKIPPED:
        return [
            f"{report.nodeid}: skipped although the [{report.extra}] extra is installed — "
            "the coverage went quiet"
        ]
    if report.outcome in (FAILED, "error"):
        return [f"{report.nodeid}: {report.outcome}"]
    if report.outcome == MISSING:
        return [
            f"{report.nodeid}: {MISSING} — collected but no report, so the gate cannot count it"
        ]
    return []


def _absent_problems(report: Report) -> list[str]:
    """The extra is gone, so skipping — naming the extra — is the only correct outcome."""
    if report.outcome == SKIPPED:
        if f"[{report.extra}]" not in report.reason:
            return [
                f"{report.nodeid}: skipped without naming [{report.extra}] — the reason has to "
                f"say which extra is missing and how to get it: "
                f"pip install 'yt-tools-cli[{report.extra}]'"
            ]
        return []
    if report.outcome == MISSING:
        return [
            f"{report.nodeid}: {MISSING} — collected but no report, so the gate cannot count it"
        ]
    return [
        f"{report.nodeid}: {report.outcome} without the [{report.extra}] extra — "
        f"a test that needs [{report.extra}] must skip, not run"
    ]
