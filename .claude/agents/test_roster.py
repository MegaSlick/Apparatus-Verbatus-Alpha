"""The custom agent roles stay read-only and keep their review depth."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = Path(__file__).parent
ROLE_FILES = sorted(AGENTS.glob("*.md"))
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "Bash"}
EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultracode": 5}
EFFORT_FLOORS = {"auditor": "high", "consult": "xhigh"}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name} has no frontmatter"
    parsed: dict[str, str] = {}
    for line in text.split("---\n", 2)[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def listed(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def test_the_roster_is_not_empty():
    # Every check below iterates the roster; an empty one would pass them all vacuously.
    assert ROLE_FILES


def test_every_role_declares_identity_model_effort_and_tools():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert data["name"] == path.stem
        for key in ("model", "effort", "tools", "disallowedTools"):
            assert data[key], f"{path.name} has no {key}"


def test_no_role_can_write_run_a_shell_or_spawn_an_agent():
    # A role file's tools apply to every dispatch of that name, so writing work goes to
    # a worktree agent under a brief instead. `Agent` counts as write access: every
    # built-in agent type can write.
    for path in ROLE_FILES:
        data = frontmatter(path)
        tools = listed(data["tools"])
        assert not tools & (WRITE_TOOLS | {"Agent"}), f"{path.name} holds {tools}"
        assert "Agent" in listed(data["disallowedTools"])


def test_turn_caps_and_memory_are_not_enabled():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert "maxTurns" not in data
        assert "memory" not in data


def test_review_roles_keep_their_effort_floors():
    for name, floor in EFFORT_FLOORS.items():
        path = AGENTS / f"{name}.md"
        assert path.exists(), f"{name}.md is missing"
        effort = frontmatter(path)["effort"]
        assert EFFORT_RANK[effort] >= EFFORT_RANK[floor], f"{name} is under its {floor} floor"


def test_project_disables_nested_subagent_fanout():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
