"""``yt-tools doctor`` — preflight with per-check status, ``can_proceed``, ``next_step``.

Contract: [[requirements:46]] D5–D8, AC4/AC5.

Doctor only *diagnoses*: it reports what is present, what is missing and the
exact command that fixes it — it never installs anything and never mutates the
cache directory it inspects (D8). A missing optional extra is ``missing`` (that
flow is unavailable) but *not* a blocker: the light flows still live (D6). Only
the two binaries the suite shells out to — ``yt-dlp`` and ``ffmpeg`` — block.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

from yt_tools import cache as cache_mod
from yt_tools import cli as umbrella_cli
from yt_tools import doctor, extras

FULL_ENV = ("scenedetect", "cv2", "librosa", "matplotlib", "rapidocr", "onnxruntime", "bpm_detector")

#: Everything a bare core install has *not* got — the extras plus the one
#: enrichment that cannot be an extra (not on PyPI, and PyPI rejects direct
#: references in metadata: release 0.23.0 died on that).
ENRICHMENT_MODULE = "bpm_detector"
NO_ENRICHMENT_ENV = tuple(m for m in FULL_ENV if m != ENRICHMENT_MODULE)
BOTH_BINS = ("yt-dlp", "ffmpeg")


def _which(present):
    """Fake ``shutil.which``: only ``present`` resolves."""

    def fake(name):
        return f"/usr/bin/{name}" if name in present else None

    return fake


def _only_importable(monkeypatch, *allowed):
    """Fake the extras import probe: ``allowed`` import, everything else raises."""

    def fake_import(name, *args, **kwargs):
        if name in allowed:
            return object()
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(extras, "import_module", fake_import)


def _by_name(report: doctor.Report) -> dict[str, doctor.Check]:
    return {c.name: c for c in report.checks}


# ---- AC4: bare core is a working install, not a broken one ------------------


def test_bare_core_reports_missing_extras_but_still_allows_light_flows(monkeypatch, tmp_path):
    _only_importable(monkeypatch)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))

    checks = _by_name(report)
    for extra in ("frames", "audio", "ocr"):
        assert checks[f"extra:{extra}"].status == "missing"
        assert checks[f"extra:{extra}"].required is False
    assert report.can_proceed is True
    assert report.blockers == ()
    assert report.next_step is not None
    assert report.next_step.startswith("pipx inject yt-tools-cli ")
    for package in ("scenedetect", "librosa", "matplotlib", "rapidocr"):
        assert package in report.next_step


def test_extras_that_are_installed_are_ok(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    for extra in ("frames", "audio", "ocr"):
        assert _by_name(report)[f"extra:{extra}"].status == "ok"
    assert report.can_proceed is True


# [test-modify: test_extra_check_reuses_the_shared_install_command: was
#  `assert fix == "pipx inject yt-tools-cli scenedetect opencv-python"`; is the
#  same via `extras.inject_command_for`; reason: task:2831 — doctor's one command
#  is now formatted by the shared helper (specs double-quoted, bounded specs like
#  the OCR pin safe to paste), so the literal here must move with it.]
def test_extra_check_reuses_the_shared_install_command(monkeypatch, tmp_path):
    _only_importable(monkeypatch)
    fix = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))["extra:frames"].fix
    assert fix == extras.inject_command("frames")
    assert fix == extras.inject_command_for(["frames"])
    assert fix == 'pipx inject yt-tools-cli "scenedetect" "opencv-python"'


# ---- AC4: a missing binary is the blocker -----------------------------------


def test_missing_ffmpeg_blocks_and_names_the_exact_command(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(
        tmp_path, which=_which(("yt-dlp",)), version_info=(3, 12, 1), platform_name="win32"
    )

    ffmpeg = _by_name(report)["ffmpeg"]
    assert ffmpeg.status == "missing"
    assert ffmpeg.required is True
    assert report.can_proceed is False
    assert [c.name for c in report.blockers] == ["ffmpeg"]
    assert report.next_step == "winget install Gyan.FFmpeg"


def test_missing_yt_dlp_blocks_and_names_the_exact_command(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(("ffmpeg",)), version_info=(3, 12, 1))

    yt_dlp = _by_name(report)["yt-dlp"]
    assert yt_dlp.status == "missing"
    assert yt_dlp.required is True
    assert report.can_proceed is False
    assert report.next_step == "pipx inject yt-tools-cli yt-dlp"


@pytest.mark.parametrize(
    ("platform_name", "command"),
    [
        ("win32", "winget install Gyan.FFmpeg"),
        ("darwin", "brew install ffmpeg"),
        ("linux", "sudo apt install ffmpeg"),
    ],
)
def test_ffmpeg_fix_is_exact_per_platform(monkeypatch, tmp_path, platform_name, command):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(("yt-dlp",)), platform_name=platform_name)
    assert report.next_step == command


def test_an_unlisted_platform_still_names_a_fix(monkeypatch, tmp_path):
    """The fallback branch: no package manager we can name, but never a silent blocker."""
    _only_importable(monkeypatch, *FULL_ENV)
    assert doctor.ffmpeg_fix("freebsd") == "install ffmpeg (see README -> Installation)"
    report = doctor.collect(tmp_path, which=_which(("yt-dlp",)), platform_name="freebsd")
    assert report.next_step == "install ffmpeg (see README -> Installation)"


def test_a_present_binary_reports_its_path(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert _by_name(report)["ffmpeg"].detail == "/usr/bin/ffmpeg"
    assert _by_name(report)["yt-dlp"].status == "ok"


# ---- D6: warn is a recommendation, never a blocker --------------------------


def test_python_above_the_supported_range_is_a_warn_not_a_blocker(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 13, 1))

    python = _by_name(report)["python"]
    assert python.status == "warn"
    assert python.required is False
    assert "3.13" in python.detail
    assert report.can_proceed is True


def test_python_below_the_supported_range_is_a_warn_too(monkeypatch, tmp_path):
    """D6 names only the two binaries as blockers: an odd interpreter is not fatal here."""
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 9, 9))
    python = _by_name(report)["python"]
    assert python.status == "warn"
    assert report.can_proceed is True


def test_a_blocker_without_a_fix_yields_no_next_step():
    """`next_step` is the contract's exact command (D6).

    If a blocker ever carries none, recommending an *extras* install while the
    environment is blocked would be worse than saying nothing
    ([[task:2799]] gap 8).
    """
    report = doctor.Report(
        checks=(
            doctor.Check("ffmpeg", doctor.MISSING, "not found on PATH", required=True),
            doctor.Check("extra:audio", doctor.MISSING, "not installed", fix="pipx inject yt-tools-cli librosa"),
        )
    )
    assert report.can_proceed is False
    assert report.next_step is None


def test_every_blocker_that_can_occur_names_a_fix(monkeypatch, tmp_path):
    """The invariant behind the fallback above: collect never emits a fix-less blocker."""
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(()), version_info=(3, 12, 1))

    assert [c.name for c in report.blockers] == ["yt-dlp", "ffmpeg"]
    assert all(c.fix for c in report.blockers)
    assert report.next_step == "pipx inject yt-tools-cli yt-dlp"


def test_a_supported_python_is_ok(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert _by_name(report)["python"].status == "ok"


# ---- D8: diagnostics only — no mutation --------------------------------------


def test_doctor_does_not_create_the_cache_dir(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert not (tmp_path / "yt-cache").exists()


def test_existing_cache_reports_its_size(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    entry = tmp_path / "yt-cache" / "abc12345"
    entry.mkdir(parents=True)
    (entry / "source.mp4").write_bytes(b"x" * 2048)

    cache = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "ok"
    assert "1 video" in cache.detail
    assert "2.0 KB" in cache.detail
    assert list(entry.iterdir()) == [entry / "source.mp4"]  # D8: the probe left no trace


def test_unwritable_cache_is_a_warn_not_a_blocker(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    (tmp_path / "yt-cache").mkdir()
    monkeypatch.setattr(doctor, "_writable", lambda path: False)

    cache = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))['cache']
    assert cache.status == "warn"
    assert cache.required is False


def test_a_base_that_does_not_exist_yet_is_not_a_false_alarm(monkeypatch, tmp_path):
    """Every flow writes with ``mkdir(parents=True)`` — a missing base is normal.

    Probing the *base* for writability would warn on a state that works: the
    nearest existing ancestor is what decides whether the chain can be created.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    cache = _by_name(doctor.collect(tmp_path / "deep" / "nested", which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "ok"
    assert "not created yet" in cache.detail


def test_unwritable_ancestor_of_a_missing_base_is_a_warn(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    monkeypatch.setattr(doctor, "_writable", lambda path: False)
    cache = _by_name(doctor.collect(tmp_path / "deep" / "nested", which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert str(tmp_path) in cache.detail


# ---- F4 ([[task:2797]]): the writability answer is a write probe, not os.access -------


def test_writability_is_probed_by_writing_not_by_os_access(monkeypatch, tmp_path):
    """``os.access(W_OK)`` is not an answer on Windows: it reads the DOS read-only
    attribute, which directories do not carry — so the branch it guarded was dead
    on the main platform ([[task:2797]] F4). Whether the check is writable must be
    decided by the real operation, not by that call.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    entry = tmp_path / "yt-cache" / "abc12345"
    entry.mkdir(parents=True)
    (entry / "source.mp4").write_bytes(b"x" * 1024)

    real_access = os.access
    consulted: list[object] = []

    def recording_access(path, mode, **kwargs):
        consulted.append(path)
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", recording_access)

    cache = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "ok"
    assert "1 video" in cache.detail
    assert consulted == []


def test_write_probe_answers_yes_and_leaves_nothing_behind(tmp_path):
    """D8: the probe is a check, not a mutation — the directory is left untouched."""
    target = tmp_path / "yt-cache"
    target.mkdir()

    assert doctor._writable(target) is True
    assert list(target.iterdir()) == []


def test_write_probe_says_no_when_the_path_is_not_a_directory(tmp_path):
    """A real answer from the real probe — reproducible on every platform."""
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x", encoding="utf-8")

    assert doctor._writable(not_a_dir) is False
    assert doctor._writable(tmp_path / "missing") is False


def test_write_probe_still_answers_yes_when_cleanup_cannot_unlink(monkeypatch, tmp_path):
    """The verdict is the creation, not the removal: a probe file that cannot be
    cleaned up (a scanner holding it) must not be reported as a read-only
    directory. ``_writable`` therefore swallows the unlink failure — drop that
    swallow and this test blows up with the PermissionError instead of answering.
    """
    target = tmp_path / "yt-cache"
    target.mkdir()
    real_unlink = os.unlink

    def denied_unlink(path, *args, **kwargs):
        if Path(path).parent == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", denied_unlink)

    assert doctor._writable(target) is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not enforce POSIX mode bits on directories (F4 itself)",
)
def test_write_probe_says_no_on_a_really_unreadable_directory(tmp_path):
    target = tmp_path / "yt-cache"
    target.mkdir()
    target.chmod(0o500)  # readable, not writable
    try:
        assert doctor._writable(target) is False
    finally:
        target.chmod(0o700)


# ---- F1 ([[task:2797]]): an unreadable subtree warns, it does not kill the report ----


def _make_cache_unreadable(monkeypatch, blocked: Path):
    """Deny reads of ``blocked`` the way a real permission wall does.

    On Linux this is exactly what ``chmod 000`` produces (the POSIX test below
    does it for real); Windows cannot deny directory reads with chmod at all
    (F4), so the denial is injected at the scan calls — the same calls the wall
    fails in. Both spellings are denied on purpose: ``os.walk`` scans, and
    ``Path.iterdir`` scans on 3.13+ but lists on 3.12 and older, so a
    scandir-only denial would silently stop exercising the root case.
    """
    real_scandir, real_listdir = os.scandir, os.listdir

    def exploding_scandir(path):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    def exploding_listdir(path="."):
        if Path(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(cache_mod.os, "scandir", exploding_scandir)
    monkeypatch.setattr(cache_mod.os, "listdir", exploding_listdir)


def test_an_unreadable_cache_subtree_undercounts_loudly_not_silently(monkeypatch, tmp_path):
    """The subdir half of F1: the readable part is still summed, but the report
    says the number is a lower bound instead of presenting it as the size.

    On a real wall the pre-fix code silently reported ``ok`` with the short sum —
    ``pathlib`` swallows the ``PermissionError`` inside glob (checked on 3.10,
    3.12 and 3.13): the wrong number looked exactly like a right one.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    entry = tmp_path / "yt-cache" / "abc12345"
    blocked = entry / "sub"
    blocked.mkdir(parents=True)
    (blocked / "source.mp4").write_bytes(b"x" * 1024)
    (entry / "other.mp4").write_bytes(b"y" * 512)
    _make_cache_unreadable(monkeypatch, blocked)

    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    cache = _by_name(report)["cache"]

    assert cache.status == "warn"  # D5: a broken environment is explained, not fatal
    assert cache.required is False
    assert report.can_proceed is True  # warn never blocks (D6)
    assert "abc12345" in cache.detail
    assert "512 B" in cache.detail  # the readable part is still reported
    assert "lower bound" in cache.detail


def test_an_unreadable_cache_root_is_a_warn_not_a_traceback(monkeypatch, tmp_path):
    """The root half of F1 — the actual crash: ``cache_list`` calls ``iterdir``
    unguarded, so an unreadable ``yt-cache`` propagated ``PermissionError`` out of
    ``doctor`` and no report was produced at all (reproduced for real on Linux).
    """
    _only_importable(monkeypatch, *FULL_ENV)
    root = tmp_path / "yt-cache"
    (root / "abc12345").mkdir(parents=True)
    _make_cache_unreadable(monkeypatch, root)

    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    cache = _by_name(report)["cache"]
    assert cache.status == "warn"
    assert cache.required is False
    assert "cannot be read" in cache.detail


def test_an_unreadable_cache_subtree_still_yields_the_whole_report(monkeypatch, tmp_path, capsys):
    """The point of F1: doctor exists to explain exactly this environment."""
    _only_importable(monkeypatch, *FULL_ENV)
    blocked = tmp_path / "yt-cache" / "abc12345"
    blocked.mkdir(parents=True)
    _make_cache_unreadable(monkeypatch, blocked)

    rc = doctor.main(
        ["--json", "--base", str(tmp_path)], which=_which(BOTH_BINS), version_info=(3, 12, 1)
    )

    payload = json.loads(capsys.readouterr().out)
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["cache"] == "warn"
    assert payload["can_proceed"] is True
    assert rc == 0  # D7: the exit code follows can_proceed, not the warn


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod 000 does not deny access on Windows (F4)",
)
def test_an_unreadable_cache_subtree_warns_on_a_real_permission_wall(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    entry = tmp_path / "yt-cache" / "abc12345"
    blocked = entry / "sub"
    blocked.mkdir(parents=True)
    (blocked / "source.mp4").write_bytes(b"x" * 1024)
    blocked.chmod(0o000)
    try:
        report = doctor.collect(tmp_path, which=_which(BOTH_BINS))
    finally:
        blocked.chmod(0o700)
    assert _by_name(report)["cache"].status == "warn"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod 000 does not deny access on Windows (F4)",
)
def test_an_unreadable_cache_root_survives_a_real_permission_wall(monkeypatch, tmp_path):
    """The genuine pre-fix crash, in the only shape that reaches it: ``iterdir``
    on a root that can be written but not read. Mode ``0o300`` is that shape —
    ``0o000`` would have been caught earlier by the old ``os.access(W_OK)`` guard,
    which is why F1 stayed hidden (and is dead on Windows anyway, F4).
    """
    _only_importable(monkeypatch, *FULL_ENV)
    root = tmp_path / "yt-cache"
    (root / "abc12345").mkdir(parents=True)
    root.chmod(0o300)
    try:
        report = doctor.collect(tmp_path, which=_which(BOTH_BINS))
    finally:
        root.chmod(0o700)
    assert _by_name(report)["cache"].status == "warn"


# ---- F3 ([[task:2797]]): a --base that cannot become a directory is a warn -----------


def test_a_base_pointing_at_a_file_is_a_warn_not_an_ok(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x", encoding="utf-8")

    cache = _by_name(doctor.collect(not_a_dir, which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert cache.required is False
    assert str(not_a_dir) in cache.detail
    assert "file" in cache.detail


def test_an_ancestor_that_is_a_file_is_a_warn_too(monkeypatch, tmp_path):
    """``mkdir(parents=True)`` cannot build a chain through a file — say so."""
    _only_importable(monkeypatch, *FULL_ENV)
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x", encoding="utf-8")

    cache = _by_name(doctor.collect(not_a_dir / "deep" / "nested", which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert str(not_a_dir) in cache.detail


def test_a_cache_root_that_is_a_file_is_a_warn_not_a_crash(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    (tmp_path / "yt-cache").write_text("x", encoding="utf-8")

    cache = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert "file" in cache.detail


# ---- m1 ([[task:2797]] review): a dangling symlink is not "not created yet" --------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating a symlink on Windows needs a privilege it may not have",
)
def test_a_dangling_symlink_cache_root_is_a_warn_with_the_reason(monkeypatch, tmp_path):
    """``exists()`` is False for a broken link, so the pre-fix code took the
    "not created yet" branch and reported ``ok`` — for a path that can never
    become a directory.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    root = tmp_path / "yt-cache"
    root.symlink_to(tmp_path / "gone")  # the target is never created

    cache = _by_name(doctor.collect(tmp_path, which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert cache.required is False
    assert "dangling symlink" in cache.detail
    assert "not created yet" not in cache.detail


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating a symlink on Windows needs a privilege it may not have",
)
def test_a_dangling_symlink_ancestor_of_the_base_is_a_warn_too(monkeypatch, tmp_path):
    """``mkdir(parents=True)`` cannot build a chain through a dead link, and the
    search for the nearest existing ancestor must stop *on* it — otherwise it
    walks past the break and blesses the path as creatable.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone")

    cache = _by_name(doctor.collect(link / "deep", which=_which(BOTH_BINS)))["cache"]
    assert cache.status == "warn"
    assert str(link) in cache.detail
    assert "dangling symlink" in cache.detail


# ---- version consistency (D5) ------------------------------------------------


def _version_files(tmp_path: Path, *, package="0.16.0", plugin="0.16.0", skill="0.16.0") -> dict[str, Path]:
    (tmp_path / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "using-yt-tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "yt-tools"\nversion = "{package}"\n', encoding="utf-8"
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "yt-tools", "version": plugin}) + "\n", encoding="utf-8"
    )
    (tmp_path / "skills" / "using-yt-tools" / "SKILL.md").write_text(
        f"---\nname: using-yt-tools\nversion: {skill}\ndescription: text\n---\n\n# body\n",
        encoding="utf-8",
    )
    return {
        "pyproject": tmp_path / "pyproject.toml",
        "plugin_json": tmp_path / ".claude-plugin" / "plugin.json",
        "skill_md": tmp_path / "skills" / "using-yt-tools" / "SKILL.md",
    }


def test_version_check_is_ok_when_every_source_agrees(tmp_path):
    files = _version_files(tmp_path)
    check = doctor.check_version(package_version="0.16.0", **files)
    assert check.status == "ok"
    assert "0.16.0" in check.detail
    assert "yt_tools.__version__" in check.detail


def test_version_check_warns_on_divergence_and_names_the_sync_script(tmp_path):
    files = _version_files(tmp_path, skill="0.6.0")
    check = doctor.check_version(package_version="0.16.0", **files)
    assert check.status == "warn"
    assert check.required is False
    assert "0.6.0" in check.detail and "SKILL.md" in check.detail
    assert check.fix == "python scripts/sync-version.py"


def test_version_check_is_silent_when_there_is_nothing_to_cross_check(tmp_path):
    """An installed wheel has no source tree next to it — the metadata is all there is.

    One source cannot *agree* with anything, so the wording says what is true:
    this is the only source visible here ([[task:2799]] gap 7).
    """
    check = doctor.check_version(package_version="0.16.0")
    assert check.status == "ok"
    assert check.detail == "0.16.0 - only visible source: yt_tools.__version__"
    assert "agree" not in check.detail


def test_metadata_divergence_from_the_tree_names_the_reinstall(tmp_path):
    """The *other* fix branch: a stale installed distribution, not a forgotten sync.

    An installed wheel carries no `plugin.json`/`SKILL.md`, so nothing derived is
    visible and the sync script is not the answer — reinstalling is.
    """
    files = _version_files(tmp_path, package="0.1.0", plugin="0.1.0", skill="0.1.0")
    check = doctor.check_version(package_version="0.2.0", pyproject=files["pyproject"])
    assert check.status == "warn"
    assert check.fix == "pipx install --force yt-tools"
    assert "0.2.0" in check.detail and "0.1.0" in check.detail


def test_malformed_derived_files_are_not_version_sources(tmp_path):
    """Anything that cannot be read as a version is absent, never a guess."""
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    wrong_type = plugin_dir / "plugin.json"
    wrong_type.write_text('{"name": "yt-tools", "version": 42}', encoding="utf-8")
    junk = plugin_dir / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    no_frontmatter = tmp_path / "no-frontmatter.md"
    no_frontmatter.write_text("# just a heading\n", encoding="utf-8")
    no_version = tmp_path / "no-version.md"
    no_version.write_text("---\nname: using-yt-tools\n---\n\n# body\n", encoding="utf-8")
    empty_version = tmp_path / "empty-version.md"
    empty_version.write_text("---\nversion:\n---\n\n# body\n", encoding="utf-8")

    assert doctor._read_plugin_json_version(wrong_type) is None
    assert doctor._read_plugin_json_version(junk) is None
    assert doctor._read_plugin_json_version(tmp_path / "absent.json") is None
    assert doctor._read_plugin_json_version(None) is None
    assert doctor._read_skill_version(no_frontmatter) is None
    assert doctor._read_skill_version(no_version) is None
    assert doctor._read_skill_version(empty_version) is None
    assert doctor._read_skill_version(tmp_path / "absent.md") is None
    assert doctor._read_skill_version(None) is None


def test_a_version_warning_never_blocks(monkeypatch, tmp_path, capsys):
    """A real divergence (not an ASCII path in a tmp dir), and the verdict on it.

    D6: only a missing *binary* blocks. A version drift is a packaging bug the
    release process owns — the flows still run, so the preflight must not refuse.
    """
    _only_importable(monkeypatch, *FULL_ENV)
    divergence = doctor.Check(
        "version",
        doctor.WARN,
        "divergence: yt_tools.__version__ says 0.2.0, pyproject.toml says 0.1.0",
        fix="pipx install --force yt-tools",
    )
    monkeypatch.setattr(doctor, "check_version", lambda **kwargs: divergence)

    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert _by_name(report)["version"].status == "warn"
    assert report.can_proceed is True
    assert report.blockers == ()

    rc = doctor.main(["--base", str(tmp_path)], which=_which(BOTH_BINS), version_info=(3, 12, 1))
    capsys.readouterr()
    assert rc == 0


# ---- AC5: --json is machine-readable, same semantics -------------------------


def test_json_report_is_valid_and_distinguishes_warn_from_missing(monkeypatch, tmp_path, capsys):
    _only_importable(monkeypatch)
    rc = doctor.main(
        ["--json", "--base", str(tmp_path)],
        which=_which(("yt-dlp",)),
        version_info=(3, 13, 0),
        platform_name="win32",
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["can_proceed"] is False
    assert payload["next_step"] == "winget install Gyan.FFmpeg"
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["python"] == "warn"
    assert statuses["ffmpeg"] == "missing"
    assert statuses["extra:frames"] == "missing"
    assert rc == 1


def test_json_flag_does_not_change_the_exit_code(monkeypatch, tmp_path, capsys):
    _only_importable(monkeypatch, *FULL_ENV)
    kwargs = dict(which=_which(BOTH_BINS), version_info=(3, 12, 1))
    human_rc = doctor.main(["--base", str(tmp_path)], **kwargs)
    json_rc = doctor.main(["--json", "--base", str(tmp_path)], **kwargs)
    capsys.readouterr()
    assert human_rc == json_rc == 0


def test_ok_report_has_no_next_step(monkeypatch, tmp_path, capsys):
    _only_importable(monkeypatch, *FULL_ENV)
    rc = doctor.main(["--json", "--base", str(tmp_path)], which=_which(BOTH_BINS), version_info=(3, 12, 1))
    payload = json.loads(capsys.readouterr().out)
    assert payload["can_proceed"] is True
    assert payload["next_step"] is None
    assert rc == 0


# ---- the enrichment that cannot be a package extra --------------------------
#
# `bpm-detector` is the difference between librosa-only BPM/key and full chord
# progression + structure in `yt-listen`, but it is not on PyPI and PyPI rejects
# PEP 508 direct references in `Requires-Dist` — so it cannot ride `[full]`
# (release 0.23.0 died on exactly that, [[task:2824]]) and is installed with
# `pipx inject` instead. Nothing in the install spec guarantees it, so doctor has
# to name it: without this line a PyPI install has no way to learn the enrichment
# exists at all. Absent is a `warn` — a feature is lost, nothing is blocked (D6).


def test_missing_bpm_detector_is_a_warn_with_the_inject_command(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *NO_ENRICHMENT_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))

    check = _by_name(report)["enrichment:bpm-detector"]
    assert check.status == "warn"
    assert check.required is False
    assert check.fix == extras.BPM_DETECTOR_FIX
    assert "git+https://github.com/libraz/bpm-detector" in check.fix
    assert report.can_proceed is True


def test_a_present_bpm_detector_is_reported_ok(monkeypatch, tmp_path):
    _only_importable(monkeypatch, *FULL_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert _by_name(report)["enrichment:bpm-detector"].status == "ok"


def test_the_enrichment_alone_becomes_the_next_step(monkeypatch, tmp_path):
    """Nothing else wrong: the one command worth running is the enrichment."""
    _only_importable(monkeypatch, *NO_ENRICHMENT_ENV)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert report.next_step == extras.BPM_DETECTOR_FIX


def test_a_missing_extra_outranks_the_enrichment(monkeypatch, tmp_path):
    """Priority stays: extras first (they unlock whole flows), then the warn."""
    _only_importable(monkeypatch)
    report = doctor.collect(tmp_path, which=_which(BOTH_BINS), version_info=(3, 12, 1))
    assert report.next_step.startswith("pipx inject yt-tools-cli ")
    assert "git+https" not in report.next_step


# ---- human output is the default --------------------------------------------


def test_human_output_names_every_check_and_the_next_step(monkeypatch, tmp_path, capsys):
    _only_importable(monkeypatch)
    rc = doctor.main(
        ["--base", str(tmp_path)], which=_which(("yt-dlp",)), version_info=(3, 12, 0), platform_name="linux"
    )

    out = capsys.readouterr().out
    assert "missing" in out and "can_proceed: no" in out
    assert "next_step: sudo apt install ffmpeg" in out
    for name in ("python", "yt-dlp", "ffmpeg", "extra:frames", "extra:audio", "extra:ocr", "enrichment:bpm-detector", "cache", "version"):
        assert name in out
    assert rc == 1


def test_human_output_template_stays_ascii(monkeypatch, tmp_path, capsys):
    """The template must survive a stream that cannot be reconfigured (capsys, a pipe).

    The live-data case is the test below; this one pins the weaker invariant that
    was the whole fix in v0.17.0 — no literal arrow in the layout.
    """
    _only_importable(monkeypatch)
    doctor.main(["--base", str(tmp_path)], which=_which(BOTH_BINS), version_info=(3, 13, 0))
    capsys.readouterr().out.encode("ascii")  # must not raise


def test_json_output_template_stays_ascii(monkeypatch, tmp_path, capsys):
    _only_importable(monkeypatch)
    doctor.main(["--json", "--base", str(tmp_path)], which=_which(BOTH_BINS), version_info=(3, 13, 0))
    capsys.readouterr().out.encode("ascii")  # must not raise


# ---- F2 ([[task:2798]]): the console must not eat the data -------------------


def cp1251_console():
    """A Windows cp1251 console in strict mode — the stream that used to crash."""
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1251", errors="strict"), raw


def base_outside_cp1251(tmp_path: Path) -> Path:
    """🎬 is not in cp1251: no ASCII template can save a report that prints this path."""
    base = tmp_path / "видео 🎬"
    (base / "yt-cache" / "abc12345").mkdir(parents=True)
    return base


def test_human_output_survives_a_path_outside_cp1251(monkeypatch, tmp_path):
    """An ASCII template is not enough: the checks print *live paths*.

    Every other CLI forces UTF-8 on its streams before printing; doctor did not,
    so a cache path with an emoji crashed the preflight on the very console it
    exists to diagnose ([[task:2798]] F2).
    """
    _only_importable(monkeypatch, *FULL_ENV)
    base = base_outside_cp1251(tmp_path)
    console, raw = cp1251_console()

    with contextlib.redirect_stdout(console):
        rc = doctor.main(["--base", str(base)], which=_which(BOTH_BINS), version_info=(3, 12, 1))
    console.flush()

    written = raw.getvalue().decode("utf-8")
    assert "🎬" in written, "the path must reach the console, not a replacement character"
    assert rc == 0


# ---- the umbrella CLI routes to it -------------------------------------------


def test_umbrella_cli_routes_doctor_and_forwards_base(monkeypatch, tmp_path, capsys):
    seen: dict[str, object] = {}
    report = doctor.Report(checks=(doctor.Check("python", doctor.OK, "3.12.1"),), version="0.16.0")

    def fake_collect(base=None, **kwargs):
        seen["base"] = base
        return report

    monkeypatch.setattr(doctor, "collect", fake_collect)
    rc = umbrella_cli.main(["doctor", "--json", "--base", str(tmp_path)])

    assert json.loads(capsys.readouterr().out)["can_proceed"] is True
    assert seen["base"] == tmp_path
    assert rc == 0
