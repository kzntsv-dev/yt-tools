"""The skill is a portable pointer, not a harness manual ([[requirements:46]] D10, AC7).

`using-yt-tools` is delivered by more than one host: the Claude Code plugin
ships it, and pi loads the same file from the repo (`.pi/settings.json`). The
body must therefore describe *what the agent does* — resolve a CLI, run a flow,
read an artefact — and treat any harness-specific mechanism as an option, never
as a step the flow depends on.

Three properties the tests below pin:

* no harness-only *requirement* (an ``mcp__*`` tool name, a plugin env var in
  the body) — those are called out in D10 as the exact anti-pattern;
* every mention of a named harness sits in a conditional sentence ("if/when"),
  so the reader never has to be on that harness to follow the flow;
* the first run is `yt-tools doctor` — the environment explains itself
  ([[task:2787]]) *before* the per-OS install prose, not instead of it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skills" / "using-yt-tools" / "SKILL.md"

# Harnesses the skill is known to run under. A mention is fine; a *requirement*
# is not, so each sentence naming one must be conditional.
HARNESS_NAMES = ("Claude Code", "claude code", "Codex", "Cursor")

CONDITIONAL = re.compile(r"\b(if|when|unless|where)\b", re.IGNORECASE)


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n"), "SKILL.md must open with a YAML frontmatter block"
    return text.split("---\n", 2)[1]


def _body(text: str) -> str:
    return text.split("---\n", 2)[2]


def _sentences(text: str) -> list[str]:
    """Sentences of the prose — paragraphs flattened first, so wrapped lines join."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        flat = " ".join(block.split())
        out.extend(s for s in re.split(r"(?<=[.!?])\s+", flat) if s.strip())
    return out


# ---- no harness-only requirements -------------------------------------------


def test_body_requires_no_mcp_tool():
    """D10 names this explicitly: the flow must not depend on `mcp__interns__*`."""
    body = _body(_text())
    assert "mcp__" not in body, "an MCP tool name in the skill body is a harness-mandatory step"


def test_body_does_not_lean_on_a_plugin_environment_variable():
    """`CLAUDE_PLUGIN_ROOT` is a Claude Code variable — the body must not need it."""
    assert "CLAUDE_PLUGIN_ROOT" not in _body(_text())


def test_frontmatter_description_is_install_neutral():
    description = _frontmatter(_text())
    assert "CLAUDE_PLUGIN_ROOT" not in description
    assert "pipx install" in description, "the trigger text should name the neutral install path"


# ---- a named harness is always an option, never the flow --------------------


def test_every_harness_mention_is_conditional():
    offenders = []
    for sentence in _sentences(_body(_text())):
        if any(name in sentence for name in HARNESS_NAMES) and not CONDITIONAL.search(sentence):
            offenders.append(" ".join(sentence.split())[:120])
    assert not offenders, "unconditional harness mention(s):\n" + "\n".join(offenders)


# ---- first run explains the environment ------------------------------------


def test_first_run_points_at_doctor_before_the_manual_binary_prose():
    body = _body(_text())
    assert "yt-tools doctor" in body, "the skill must name the preflight ([[task:2787]])"
    doctor_at = body.index("yt-tools doctor")
    manual_at = body.index("winget install Gyan.FFmpeg")
    assert doctor_at < manual_at, "name `yt-tools doctor` before the per-OS install prose"


def test_first_run_documents_the_machine_form():
    body = _body(_text())
    assert "doctor --json" in body
    assert "can_proceed" in body, "the agent must be able to branch on the report"
