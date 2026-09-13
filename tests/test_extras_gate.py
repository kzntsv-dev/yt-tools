"""The extras gate's verdict logic — one rule per way a leg can lie ([[task:2842]]).

The suite's contract with CI is narrow and explicit: a test that needs an extra
skips when the extra is absent, and *runs* when it is present. Everything the
gate has to catch follows from negating those two halves, plus the failure that
started all of this ([[issue:75]]): a gated test that ran without its extra and
took the leg down with it — or worse, ran and passed while the extra's absence
went unnoticed.

Kept as a pure function so the rules are pinned by tests instead of by
inspecting a red CI run, and so the gate can be reasoned about on a machine
that has every extra installed.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.extras_gate import (
    FAILED,
    GATED_MINIMUM,
    MISSING,
    PASSED,
    SKIPPED,
    Report,
    outcome_from_report,
    session_reports,
    violations,
)


def _report(
    when: str = "call",
    *,
    passed: bool = False,
    failed: bool = False,
    skipped: bool = False,
    longrepr: object = "",
) -> SimpleNamespace:
    """The slice of a pytest TestReport these rules read."""
    return SimpleNamespace(when=when, passed=passed, failed=failed, skipped=skipped, longrepr=longrepr)


def _r(nodeid: str, extra: str, outcome: str, reason: str = "") -> Report:
    return Report(nodeid=nodeid, extra=extra, outcome=outcome, reason=reason)


AUDIO_SKIP = _r(
    "tests/test_listen.py::TestEstimateKeyFromChroma::test_pure_c_major_triad",
    "audio",
    SKIPPED,
    "the [audio] extra is not installed (librosa, matplotlib not importable) - "
    "pip install 'yt-tools-cli[audio]'",
)
FRAMES_SKIP = _r(
    "tests/test_frames.py::test_scene_mode_caps_at_default_max_frames",
    "frames",
    SKIPPED,
    "the [frames] extra is not installed (scenedetect, cv2 not importable) - "
    "pip install 'yt-tools-cli[frames]'",
)
AUDIO_PASS = _r("tests/test_listen.py::test_run_pipeline_smoke", "audio", PASSED)
FRAMES_PASS = _r("tests/test_frames.py::test_scene_mode_below_cap_is_untouched", "frames", PASSED)


class TestCoreOnlyLeg:
    """Extra absent: skip, with the extra named. Anything else is a lie."""

    def test_a_core_only_leg_with_named_skips_is_green(self):
        assert violations([AUDIO_SKIP, FRAMES_SKIP], installed=set(), minimums={}) == []

    def test_a_gated_test_that_ran_without_its_extra_is_a_violation(self):
        # The regression this task exists for: the extra is gone, the test still
        # ran. Whether it passed or failed, the gate is not doing its job — and
        # a *pass* is the quieter, more dangerous half.
        problems = violations([AUDIO_PASS, FRAMES_SKIP], installed=set(), minimums={})

        assert len(problems) == 1
        assert AUDIO_PASS.nodeid in problems[0]
        assert "without the [audio] extra" in problems[0]

    def test_a_gated_test_that_failed_without_its_extra_is_a_violation(self):
        # [[issue:75]] itself: `import librosa` in the body reddened the core leg.
        broken = _r("tests/test_listen.py::test_run_pipeline_smoke", "audio", FAILED)
        problems = violations([broken], installed=set(), minimums={})

        assert len(problems) == 1
        assert FAILED in problems[0] and "without the [audio] extra" in problems[0]

    def test_a_skip_that_does_not_name_the_extra_is_a_violation(self):
        # "3 skipped" tells a contributor nothing; naming the extra — and the
        # command that installs it — is the whole point of skipping instead of
        # failing. A skip for some *other* reason on a gated test is a violation
        # too: the gate cannot judge a leg it cannot decode.
        no_reason = _r("tests/test_frames.py::test_x", "frames", SKIPPED)
        other_reason = _r("tests/test_frames.py::test_x", "frames", SKIPPED, "no network")

        assert "without naming [frames]" in violations([no_reason], set(), minimums={})[0]
        assert "without naming [frames]" in violations([other_reason], set(), minimums={})[0]
        assert violations([FRAMES_SKIP], set(), minimums={}) == []

    def test_a_gated_test_that_never_reported_is_a_violation(self):
        # Collected, then dropped (deselected, collection error in a sibling) —
        # the gate cannot count what it never sees.
        problems = violations([_r("tests/test_frames.py::test_x", "frames", MISSING)], installed=set(), minimums={})

        assert len(problems) == 1
        assert MISSING in problems[0]


class TestLegWithExtrasInstalled:
    """Extra present: run. A skip here means the test silently stopped covering."""

    def test_an_installed_extra_whose_gated_test_passed_is_green(self):
        assert violations([AUDIO_PASS, FRAMES_PASS], installed={"audio", "frames"}, minimums={}) == []

    def test_a_gated_test_skipped_although_its_extra_is_installed_is_a_violation(self):
        problems = violations([AUDIO_PASS, FRAMES_SKIP], installed={"audio", "frames"}, minimums={})

        assert len(problems) == 1
        assert FRAMES_SKIP.nodeid in problems[0]
        assert "[frames] extra is installed" in problems[0]

    def test_a_gated_test_that_failed_is_a_violation(self):
        problems = violations([_r("tests/test_frames.py::test_x", "frames", FAILED)], installed={"frames"}, minimums={})

        assert len(problems) == 1
        assert "failed" in problems[0]

    def test_each_extra_is_judged_on_its_own(self):
        # audio installed, frames absent: one leg, two answers.
        problems = violations([AUDIO_PASS, FRAMES_SKIP], installed={"audio"}, minimums={})

        assert problems == []


class TestSessionReports:
    """Which of the session's collected gated tests this run is allowed to judge.

    A subset run (`-k`, `-m`, `-x`, a single file) must not be failed for the
    tests it deliberately did not select — that would make the gate unusable on
    a contributor's laptop, which is where the bug class was first felt.
    """

    def test_only_selected_tests_are_judged(self):
        gated = {"tests/test_frames.py::test_a": "frames", "tests/test_frames.py::test_b": "frames"}
        outcomes = {"tests/test_frames.py::test_a": (SKIPPED, "no [frames] here")}

        reports = session_reports(gated, outcomes, ["tests/test_frames.py::test_a"], strict=True)

        assert reports == [Report("tests/test_frames.py::test_a", "frames", SKIPPED, "no [frames] here")]

    def test_a_selected_test_without_a_report_is_a_violation_only_in_strict_mode(self):
        gated = {"tests/test_frames.py::test_a": "frames"}

        assert session_reports(gated, {}, ["tests/test_frames.py::test_a"], strict=False) == []
        assert session_reports(gated, {}, ["tests/test_frames.py::test_a"], strict=True) == [
            Report("tests/test_frames.py::test_a", "frames", MISSING, "")
        ]


class TestExpectedExtras:
    """The leg's own claim about what it installed — the other half of the gate."""

    def test_a_delivered_extra_raises_nothing(self):
        assert violations([FRAMES_PASS], {"frames"}, expected={"frames": []}, minimums={}) == []

    def test_an_extra_that_did_not_arrive_is_a_violation(self):
        problems = violations([], set(), expected={"frames": ["cv2"]}, minimums={})

        assert len(problems) == 1
        assert "[frames]" in problems[0] and "cv2" in problems[0]
        assert "did not deliver" in problems[0]

    def test_a_leg_that_claims_nothing_is_judged_only_on_what_it_ran(self):
        assert violations([FRAMES_SKIP], set(), expected={}, minimums={}) == []
        assert violations([FRAMES_SKIP], set(), minimums={}) == []


class TestSessionFinish:
    """The wiring, not the rules: a violation has to leave the process non-zero.

    The rules are pure and covered above; this pins the half that turns them into
    a red CI leg. It is the difference between a gate and a comment — if
    ``session.exitstatus`` stops being honoured, every other test here still
    passes and CI stays green on a broken gate.
    """

    @staticmethod
    def _session(
        items: list[str], *, collectonly: bool = False, strict: bool = False, expect: str = ""
    ) -> SimpleNamespace:
        options = {"--extras-gate-floors": strict, "--extras-gate-expect": expect}
        return SimpleNamespace(
            config=SimpleNamespace(
                option=SimpleNamespace(collectonly=collectonly, setuponly=False, setup_plan=False),
                getoption=lambda name: options.get(name, ""),
                pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
            ),
            items=[SimpleNamespace(nodeid=nodeid) for nodeid in items],
            exitstatus=0,
        )

    @staticmethod
    def _claim(monkeypatch, available: set[str]) -> None:
        """Make the extras probe answer ``available`` (both halves of it)."""
        import tests.conftest as conftest

        monkeypatch.setattr(conftest.extras, "extra_available", lambda extra, modules=None: extra in available)
        monkeypatch.setattr(
            conftest.extras,
            "missing_modules",
            lambda extra, modules=None: [] if extra in available else ["cv2"],
        )

    def test_a_violation_turns_the_session_red(self, monkeypatch):
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {"tests/test_frames.py::test_x": (PASSED, "")})
        monkeypatch.setattr(conftest.extras, "extra_available", lambda extra, modules=None: False)
        session = self._session(["tests/test_frames.py::test_x"])

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 1, "a gated test that ran without its extra must fail the leg"

    def test_a_clean_core_only_leg_leaves_the_exit_status_alone(self, monkeypatch):
        import tests.conftest as conftest

        skipped = (SKIPPED, "the [frames] extra is not installed (scenedetect, cv2 not importable)")
        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {"tests/test_frames.py::test_x": skipped})
        monkeypatch.setattr(conftest, "GATED_MINIMUM", {"frames": 1})
        monkeypatch.setattr(conftest.extras, "extra_available", lambda extra, modules=None: False)
        session = self._session(["tests/test_frames.py::test_x"], strict=True)

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 0

    def test_an_extra_with_its_gated_tests_installed_is_green(self, monkeypatch):
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_ocr_engine.py::test_x": "ocr"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {"tests/test_ocr_engine.py::test_x": (PASSED, "")})
        monkeypatch.setattr(conftest, "GATED_MINIMUM", {"ocr": 1})
        monkeypatch.setattr(conftest.extras, "extra_available", lambda extra, modules=None: True)
        session = self._session(["tests/test_ocr_engine.py::test_x"], strict=True)

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 0

    def test_a_leg_that_installs_an_extra_it_did_not_get_is_red(self, monkeypatch):
        # The [[issue:77]] shape: the leg says it installs [frames], the install
        # silently did not deliver cv2, every gated test skips "legitimately" and
        # the job would otherwise be green on nothing.
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(
            conftest,
            "_OUTCOMES",
            {"tests/test_frames.py::test_x": (SKIPPED, "the [frames] extra is not installed")},
        )
        monkeypatch.setattr(conftest, "GATED_MINIMUM", {"frames": 1})
        self._claim(monkeypatch, available=set())
        session = self._session(["tests/test_frames.py::test_x"], strict=True, expect="frames")

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 1

    def test_a_leg_that_installs_an_extra_it_did_get_is_green(self, monkeypatch):
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {"tests/test_frames.py::test_x": (PASSED, "")})
        monkeypatch.setattr(conftest, "GATED_MINIMUM", {"frames": 1})
        self._claim(monkeypatch, available={"frames"})
        session = self._session(["tests/test_frames.py::test_x"], strict=True, expect="frames")

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 0

    def test_a_typo_in_the_legs_claim_is_red_not_ignored(self, monkeypatch):
        # Otherwise `expect: framez` would quietly assert nothing at all.
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {"tests/test_frames.py::test_x": (PASSED, "")})
        monkeypatch.setattr(conftest, "GATED_MINIMUM", {"frames": 1})
        self._claim(monkeypatch, available={"frames"})
        session = self._session(["tests/test_frames.py::test_x"], strict=True, expect="framez")

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 1

    def test_a_collection_is_not_a_run(self, monkeypatch):
        import tests.conftest as conftest

        monkeypatch.setattr(conftest, "_GATED", {"tests/test_frames.py::test_x": "frames"})
        monkeypatch.setattr(conftest, "_OUTCOMES", {})
        session = self._session([], collectonly=True, strict=True)

        conftest.pytest_sessionfinish(session, 0)

        assert session.exitstatus == 0, "--collect-only has no outcomes to judge"


class TestOutcomeFromReport:
    """The hook's half: one pytest report → the gate's verdict vocabulary."""

    def test_a_skip_carries_the_reason_the_suite_printed(self):
        # pytest hands a skip's reason over as (path, lineno, reason).
        outcome, reason = outcome_from_report(_report("setup", skipped=True, longrepr=("f.py", 12, "no [frames] here")))

        assert outcome == SKIPPED
        assert reason == "no [frames] here"

    def test_a_pass_is_only_a_verdict_at_call_time(self):
        # setup/teardown also report ``passed``; counting those would double a
        # test, and a teardown-only pass must not invent one.
        assert outcome_from_report(_report("setup", passed=True)) is None
        assert outcome_from_report(_report("teardown", passed=True)) is None
        assert outcome_from_report(_report("call", passed=True)) == (PASSED, "")

    def test_a_call_failure_is_failed_and_a_setup_failure_is_an_error(self):
        assert outcome_from_report(_report("call", failed=True, longrepr="boom"))[0] == FAILED
        assert outcome_from_report(_report("setup", failed=True, longrepr="boom"))[0] == "error"

    def test_a_reason_that_is_not_the_usual_triple_still_arrives_as_text(self):
        outcome, reason = outcome_from_report(_report("setup", skipped=True, longrepr="plain text"))

        assert (outcome, reason) == (SKIPPED, "plain text")


class TestGatedSetFloor:
    """A gate that quietly loses its tests is a gate that stops gating."""

    def test_a_dwindled_gated_set_is_a_violation(self):
        # Two [frames] tests where the suite has dozens: someone deleted the
        # fixture (and with it the marker) from the rest. The leg would be green
        # and blank — which is exactly the "молча пропустить всё" failure.
        reports = [FRAMES_SKIP, _r("tests/test_frames.py::test_y", "frames", SKIPPED, "no [frames] here")]
        problems = violations(reports, installed=set(), minimums={"frames": 45})

        assert len(problems) == 1
        assert "frames" in problems[0] and "45" in problems[0]
        assert "2" in problems[0]

    def test_an_extra_whose_gated_tests_all_vanished_is_a_violation(self):
        # The extreme case the floor exists for: the [audio] gate is gone
        # entirely, so no report names it and only the floor can notice.
        reports = [
            _r(f"tests/test_frames.py::test_{i}", "frames", SKIPPED, "no [frames] here")
            for i in range(50)
        ]

        problems = violations(reports, installed=set(), minimums={"frames": 45, "audio": 8})

        assert len(problems) == 1
        assert "audio" in problems[0] and "0" in problems[0]

    def test_the_shipped_floor_covers_every_gated_extra(self):
        # The default table is the one CI enforces: an extra missing from it
        # would be a gate with no floor at all.
        assert set(GATED_MINIMUM) == {"frames", "audio", "ocr"}
        assert all(count > 0 for count in GATED_MINIMUM.values())

    def test_no_floors_means_no_floor_check(self):
        # Subset legs (the rapidocr matrix runs two files) must not be judged on
        # the whole suite's counts — the CI legs that run everything opt in.
        assert violations([FRAMES_SKIP], installed=set(), minimums={}) == []
