from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = Path(__file__).parent
ROLE_FILES = sorted(path for path in AGENTS.glob("*.md") if path.name != "README.md")
READ_ONLY = {"scout", "auditor", "consult"}


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


def test_read_only_roles_have_no_write_or_shell_tools():
    for path in ROLE_FILES:
        if path.stem not in READ_ONLY:
            continue
        tools = listed(frontmatter(path)["tools"])
        assert not tools & {"Write", "Edit", "NotebookEdit", "Bash"}


def test_turn_caps_and_memory_are_not_enabled():
    for path in ROLE_FILES:
        data = frontmatter(path)
        assert "maxTurns" not in data
        assert "memory" not in data


def test_project_disables_nested_subagent_fanout():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
