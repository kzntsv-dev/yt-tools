#!/usr/bin/env bash
# Reproducible gate for AC4/AC5 of [[requirements:46]] — "core without extras +
# `yt-tools doctor`", the half of the install contract that a pytest run cannot
# see: the tests mock `shutil.which` and the extras probe, so nothing in them
# proves that a *really* bare install still reports `missing` and still blocks on
# a missing `ffmpeg`.
#
# The gate runs itself inside a clean container: the outer invocation only mounts
# the checkout and re-executes this same file with `--inside`. One file, no
# quoting-through-`bash -c`, and the inner half is a plain script that also works
# when CI runs `bash /src/scripts/check-core-install.sh --inside` directly.
#
#   bash scripts/check-core-install.sh              # docker, from anywhere
#   YT_SRC=/path/to/checkout bash scripts/check-core-install.sh   # another tree
#   YT_GATE_IMAGE=python:3.11-slim bash ...          # another interpreter
#
# Exit code: 0 only when A, B and C all hold. Any mismatch names the failing
# block and the exact expectation — this script is the verifier, so "failed" is
# not an acceptable output.
set -uo pipefail

IMAGE="${YT_GATE_IMAGE:-python:3.12-slim}"

# ---------------------------------------------------------------------------
# inside the container
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--inside" ]; then
    set -u
    rc=0
    report() { echo "gate: FAIL $1: $2"; rc=1; }

    # A and B assert *opposite* values in the same run (can_proceed=false/rc=1
    # then can_proceed=true/rc=0), so a gate that always passes cannot exist.
    echo "== block A: core without extras, no ffmpeg =="
    if command -v ffmpeg >/dev/null 2>&1; then
        report A "ffmpeg is present in the image ($(command -v ffmpeg)); the no-ffmpeg half cannot be exercised"
    fi

    python3 -m pip install --quiet --disable-pip-version-check -e /src || {
        echo "gate: FAIL setup: pip install -e /src did not succeed"
        exit 1
    }
    echo "version: $(python3 -c 'import yt_tools; print(yt_tools.__version__)')"

    yt-tools doctor >/tmp/a.txt 2>&1
    a_rc=$?
    yt-tools doctor --json >/tmp/a.json 2>/tmp/a.err
    a_json_rc=$?

    python3 - /tmp/a.json /tmp/a.txt "$a_rc" "$a_json_rc" <<'PY'
import json
import sys

json_path, text_path, rc, json_rc = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
problems = []


def want(ok, message):
    if not ok:
        problems.append(message)


try:
    payload = json.load(open(json_path, encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    problems.append(f"`doctor --json` is not valid JSON ({exc})")
    payload = {"checks": []}

text = open(text_path, encoding="utf-8").read()
statuses = {c["name"]: c["status"] for c in payload.get("checks", [])}
required = {c["name"]: c.get("required") for c in payload.get("checks", [])}

want(rc == 1, f"doctor rc={rc}, want 1 (a missing required binary must block)")
want(json_rc == 1, f"`doctor --json` rc={json_rc}, want 1 (D7: --json changes the form, not the code)")
want(payload.get("can_proceed") is False, f"can_proceed={payload.get('can_proceed')!r}, want False")
step = payload.get("next_step") or ""
want("ffmpeg" in step, f"next_step={step!r} does not name the ffmpeg fix")
want(statuses.get("ffmpeg") == "missing", f"ffmpeg status={statuses.get('ffmpeg')!r}, want 'missing'")
want(required.get("ffmpeg") is True, "ffmpeg must be a required check")
want(statuses.get("yt-dlp") == "ok", f"yt-dlp status={statuses.get('yt-dlp')!r}, want 'ok' (it is a core dependency)")
for extra in ("extra:frames", "extra:audio", "extra:ocr"):
    want(statuses.get(extra) == "missing", f"{extra} status={statuses.get(extra)!r}, want 'missing' (bare core)")
    want(required.get(extra) is False, f"{extra} must not be required (D6: an extra is not a blocker)")
want("can_proceed: no" in text, "the human report does not say `can_proceed: no`")
want("-> " in text, "the human report names no fix (`->` line)")
want("ffmpeg" in text, "the human report does not mention ffmpeg at all")

for problem in problems:
    print(f"gate: FAIL A: {problem}")
sys.exit(1 if problems else 0)
PY
    [ $? -eq 0 ] || rc=1

    echo "== block B: real ffmpeg, extras still absent =="
    python3 -m pip install --quiet --disable-pip-version-check imageio-ffmpeg || \
        report B "imageio-ffmpeg did not install (the static ffmpeg is the fixture)"
    ffmpeg_bin="$(python3 -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null)" || \
        report B "imageio-ffmpeg does not expose a binary"
    if [ -n "${ffmpeg_bin:-}" ]; then
        cp "$ffmpeg_bin" /usr/local/bin/ffmpeg && chmod +x /usr/local/bin/ffmpeg
    fi
    ffmpeg -version >/dev/null 2>&1 || report B "the real ffmpeg binary is not runnable (the AC4 fixture must be real)"

    yt-tools doctor >/tmp/b.txt 2>&1
    b_rc=$?
    yt-tools doctor --json >/tmp/b.json 2>/tmp/b.err
    b_json_rc=$?

    python3 - /tmp/b.json /tmp/b.txt "$b_rc" "$b_json_rc" <<'PY'
import json
import sys

json_path, text_path, rc, json_rc = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
problems = []


def want(ok, message):
    if not ok:
        problems.append(message)


payload = json.load(open(json_path, encoding="utf-8"))
text = open(text_path, encoding="utf-8").read()
statuses = {c["name"]: c["status"] for c in payload.get("checks", [])}

want(rc == 0, f"doctor rc={rc}, want 0 (nothing required is missing now)")
want(json_rc == 0, f"`doctor --json` rc={json_rc}, want 0")
want(payload.get("can_proceed") is True, f"can_proceed={payload.get('can_proceed')!r}, want True")
want(statuses.get("ffmpeg") == "ok", f"ffmpeg status={statuses.get('ffmpeg')!r}, want 'ok'")
for extra in ("extra:frames", "extra:audio", "extra:ocr"):
    want(statuses.get(extra) == "missing", f"{extra} status={statuses.get(extra)!r}, want 'missing'")
want("can_proceed: yes" in text, "the human report does not say `can_proceed: yes`")
step = payload.get("next_step") or ""
want("inject" in step or "pip install" in step,
     f"next_step={step!r} does not name the extras install command")

for problem in problems:
    print(f"gate: FAIL B: {problem}")
sys.exit(1 if problems else 0)
PY
    [ $? -eq 0 ] || rc=1

    echo "== block C: warn and missing are distinguishable in one payload =="
    printf 'x' >/tmp/a-file
    yt-tools doctor --json --base /tmp/a-file >/tmp/c.json 2>/tmp/c.err
    python3 - /tmp/c.json <<'PY'
import json
import sys

problems = []


def want(ok, message):
    if not ok:
        problems.append(message)


payload = json.load(open(sys.argv[1], encoding="utf-8"))
statuses = {c["name"]: c["status"] for c in payload.get("checks", [])}
asserted = set(statuses.values())

want(statuses.get("cache") == "warn", f"cache status={statuses.get('cache')!r}, want 'warn' (--base is a file)")
want("missing" in asserted, "no `missing` check in the same payload to compare against")
want("warn" in asserted, "no `warn` check in the same payload")
want("warn" != "missing" and {"warn", "missing"} <= asserted,
     "`warn` and `missing` do not coexist, so machine distinguishability is untested")
# ffmpeg *is* installed by now and an extra is never required, so the only non-ok
# statuses are warns - and a warn must not block (D6). B already proved rc=1 when
# a required binary is missing; this is the mirror of that.
want(payload.get("can_proceed") is True, "a warn must not block: can_proceed should stay True here")

for problem in problems:
    print(f"gate: FAIL C: {problem}")
sys.exit(1 if problems else 0)
PY
    [ $? -eq 0 ] || rc=1

    if [ "$rc" -eq 0 ]; then
        echo "gate: PASS (A rc=1/can_proceed=false, B rc=0/can_proceed=true, C warn+missing distinguishable)"
    else
        echo "gate: FAILED - see the named block above"
    fi
    exit "$rc"
fi

# ---------------------------------------------------------------------------
# outer: mount the checkout and re-run this file inside the container
# ---------------------------------------------------------------------------

src="$(cd "${YT_SRC:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
mount_src="$src"
if command -v cygpath >/dev/null 2>&1; then
    # git-bash: MSYS rewrites POSIX-looking arguments, and docker needs a Windows
    # path for the bind mount — the same two-line dance the other dev tooling does.
    mount_src="$(cygpath -w "$src")"
    export MSYS_NO_PATHCONV=1
fi

command -v docker >/dev/null 2>&1 || {
    echo "gate: docker is not on PATH - this gate runs the install in a clean container" >&2
    exit 2
}

echo "gate: $src in $IMAGE"
exec docker run --rm -v "${mount_src}:/src" "$IMAGE" bash /src/scripts/check-core-install.sh --inside
