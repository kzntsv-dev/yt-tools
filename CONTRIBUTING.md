# Contributing

Thanks for looking. yt-tools is a small, opinionated toolkit: nine stateless CLIs
for agent-driven YouTube watching. The rules below exist to keep it that way.

## Getting set up

```bash
git clone https://github.com/kzntsv-dev/yt-tools.git
cd yt-tools
pip install -e ".[full,test]"   # [full] = frames + audio stacks; [ocr] is separate
pytest -q
```

`ffmpeg` and `yt-dlp` are external binaries the tools shell out to; `yt-tools
doctor` reports what is present and the exact command for what is not.

Two gates are worth knowing before you push:

```bash
pytest -q                              # unit + CLI smoke (network is mocked)
bash scripts/check-core-install.sh     # needs docker: the install contract, end to end
```

## What a change should look like

- **Test first.** A behaviour change comes with a test that fails without it. If
  you cannot make the test fail on the current code, it is not testing the change.
  A test that cannot fail (a "guard") is welcome, but say so in its docstring.
- **Explain the refusal.** Absence of an optional dependency is a message with the
  exact command, never a traceback. The same goes for a broken environment: report
  it and exit non-zero.
- **Say what you know and what you do not.** If a number can only be partially
  computed, qualify it — an undercount presented as an exact figure is the bug
  class this project has already been bitten by.
- **Keep the core light.** Anything heavy belongs in an extra. `pip install
  yt-tools` must stay a seconds-long install.
- **Add an entry** to `CHANGELOG.md` under `[Unreleased]`, and bump `version` in
  `pyproject.toml` (see below).

## Style

- Python 3.10+ syntax, `from __future__ import annotations` at the top.
- Comments and docstrings explain *why* — ideally with the failure that motivated
  them. "What" is the code's job.
- Human-facing output templates stay ASCII: the console may be cp1251 on Windows.
- Keep output parseable: `--json` where a machine reads it, stable wording where a
  person does.

## Releases

The version lives in exactly one place — `pyproject.toml`. The derived files are
written by a script, never by hand:

```bash
# 1. bump `version` in pyproject.toml
python scripts/sync-version.py          # plugin.json + skill frontmatter
python scripts/sync-version.py --check  # what the guard test runs
# 2. add the CHANGELOG entry, commit, push
git commit -m "feat(scope): what changed (vX.Y.Z)"
```

`tests/test_version.py` fails when the sources disagree, so a half-finished bump
cannot ship.

## Reporting a bug

Include what you ran, what you expected, what happened (the exact message), your
platform, and the output of `yt-tools doctor`. Issues that reproduce on a bare
`pip install yt-tools` are the most valuable ones.
