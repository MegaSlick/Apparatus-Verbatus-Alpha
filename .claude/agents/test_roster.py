from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = Path(__file__).parent
ROLE_FILES = sorted(path for path in AGENTS.glob("*.md") if path.name != "README.md")


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    block = text.split("---\n", 2)[1]
    parsed: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def listed(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def writes(path: Path) -> bool:
    # Bash counts: a role holding only a shell can still edit any file through it.
    return bool(listed(frontmatter(path)["tools"]) & {"Write", "Edit", "NotebookEdit", "Bash"})


def test_the_roster_is_not_empty():
    # Not a list of names: which roles exist is configuration Tyrel changes, and a
    # test that pins the set makes deleting a role he does not want fail a check for
    # no protective reason. What must hold is that the roster has entries at all —
    # every bound below iterates ROLE_FILES, so an empty roster would pass them all
    # vacuously and report a green suite over no enforcement.
    assert ROLE_FILES, "no agent definitions found; every capability bound below is vacuous"


def test_every_role_declares_identity_model_effort_and_tools():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert data["name"] == path.stem
        assert data["model"]
        assert data["effort"]
        assert data["tools"]
        assert data["disallowedTools"]


def test_agents_cannot_spawn_more_agents():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert "Agent" not in listed(data["tools"])
        assert "Agent" in listed(data["disallowedTools"])


def test_turn_caps_and_memory_are_not_enabled():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert "maxTurns" not in data
        assert "memory" not in data


def test_project_disables_nested_subagent_fanout():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


def test_no_host_role_can_write_or_run_a_shell():
    # The bound that replaced three separate ones. `worker`, `infra-worker` and
    # `rebuilder` held `Write`, `Edit` and `Bash` on Tyrel's machine — the session's
    # own reach, granted to something running unattended against a prompt nobody
    # reads twice — and each carried its own prohibitions in prose to compensate.
    # They are briefs now (`operations/autoclave/briefs/`), dispatched into a
    # container where the boundary is the mount rather than the wording.
    #
    # Stated as a bound on the roster rather than as a fact about today's roles:
    # this is what fails if a writing role is ever added back here instead.
    for path in ROLE_FILES:
        assert not writes(path), (
            f"{path.name} holds a write or shell tool. Writing work is dispatched into a "
            "chamber — see operations/autoclave/README.md — not spawned on this machine."
        )


def test_the_briefs_that_replaced_the_writing_roles_still_bind_them():
    # The prohibition did not disappear with the role files; it moved. A brief is
    # what a dispatched agent is actually given, so it is where the rule has to be —
    # nothing in the container enforces it, which is exactly why the wording matters.
    briefs = sorted((ROOT / "operations" / "autoclave" / "briefs").glob("*.md"))
    roles = [path for path in briefs if path.name != "README.md"]
    assert roles, "no role briefs found; the writing roles have nowhere to be dispatched from"
    for path in roles:
        text = path.read_text(encoding="utf-8").lower()
        assert "never edit a governed path" in text, (
            f"{path.name} never states the governed-path prohibition"
        )
        assert ".claude/" in text, (
            f"{path.name} states the prohibition without naming `.claude/`, which is the "
            "half of it an agent is most likely to reach"
        )
        assert "propose" in text and "exact wording" in text, (
            f"{path.name} never routes document changes through a proposal"
        )


def test_judgement_roles_keep_their_effort_floors():
    # Floors, not pins: frontmatter may exceed the floor, never sit under it. A
    # review must not quietly run at a cheap session's depth. Fail closed: a
    # judgement seat cannot shed its floor by deletion or rename — removing one
    # is a reviewed change that edits this dict in the same commit.
    rank = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
    # Duplicated in README.md's roster table on purpose; change both together.
    # Tyrel's ruling, 2026-08-01: medium is the default, and high or above is a
    # deliberate choice reserved for planning and for judging. The chamber briefs
    # build from a written spec, so they sit at medium and are raised per dispatch
    # when a unit earns it — and a brief carries no effort field at all, because a
    # value written into prose is a value nothing enforces. The two that keep
    # floors are the two that *judge*: a blind review seat and a design objection.
    # A cheap review is the one place thinness does not show until much later.
    floors = {"auditor": "high", "consult": "xhigh"}
    for name, floor in floors.items():
        path = AGENTS / f"{name}.md"
        assert path.exists(), (
            f"{name}.md is missing; a judgement seat cannot shed its floor silently"
        )
        effort = frontmatter(path)["effort"]
        assert rank[effort] >= rank[floor], (
            f"{name} declares effort {effort}, under its floor {floor}"
        )
