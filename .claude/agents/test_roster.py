"""The roster's own claims, asserted.

Hard rule 10 — a spawned agent never edits the governing documents — currently has
NO mechanical enforcement. The guard inspects Bash and MCP calls only, so `Write`
and `Edit` are never classified, and `permissions.deny` is empty. That gap is a
live finding for Tyrel to rule on.

Until it is closed, the rule exists in exactly one place: the words in the role
files that hold write tools. Words are a weak guard, but a word that silently goes
missing is no guard at all — so these tests assert the words are present. They do
not make the rule true; they make its absence loud.
"""

import re
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parent
ROLES = sorted(p for p in AGENTS.glob("*.md") if p.name != "README.md")

GOVERNING = (
    "CLAUDE.md",
    "GOALS.md",
    "GOVERNANCE.md",
    "ARCHITECTURE.md",
    "GLOSSARY.md",
    "README.md",
)


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path.name} has no frontmatter block"
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def tools_of(path):
    declared = frontmatter(path).get("tools", "")
    return {t.strip() for t in declared.split(",") if t.strip()}


def test_the_roster_is_not_empty():
    assert ROLES, "no role files found — did the roster move?"


@pytest.mark.parametrize("path", ROLES, ids=lambda p: p.stem)
def test_every_role_pins_its_effort(path):
    # Effort is the field that must never be inherited by accident: a review
    # that quietly runs at a cheap session's depth is not the review it claims.
    assert frontmatter(path).get("effort"), f"{path.name} declares no effort"


@pytest.mark.parametrize("path", ROLES, ids=lambda p: p.stem)
def test_a_role_that_can_write_is_told_not_to_touch_the_governing_documents(path):
    # The roles without Write or Edit cannot break the rule, so they are not
    # asked to carry it. The ones that can, must say so in their own file — an
    # agent reads its role file, not this test.
    tools = tools_of(path)
    if not ({"Write", "Edit"} & tools):
        pytest.skip(f"{path.stem} holds no write tools")

    body = path.read_text(encoding="utf-8").lower()
    said_dont = any(
        phrase in body
        for phrase in ("never edit a governing", "do not touch: canonical documents")
    )
    assert said_dont, (
        f"{path.name} can Write and Edit but never tells the agent to leave the "
        "governing documents alone. Nothing mechanical stops it, so the sentence "
        "in this file is the entire guard."
    )


@pytest.mark.parametrize("path", ROLES, ids=lambda p: p.stem)
def test_a_writing_role_says_to_propose_rather_than_amend(path):
    # Half the rule is "do not edit". The other half is what to do instead, and
    # an agent given only the prohibition tends to route around it — silently
    # dropping the change, or making it somewhere adjacent.
    if not ({"Write", "Edit"} & tools_of(path)):
        pytest.skip(f"{path.stem} holds no write tools")
    body = path.read_text(encoding="utf-8").lower()
    assert "propose" in body or "report" in body, (
        f"{path.name} forbids editing the governing documents but never says to "
        "propose the change instead"
    )


def test_no_role_grants_itself_the_agent_tool():
    # An agent that spawns agents makes a fan-out nobody declared and nobody can
    # cost. Stated in .claude/agents/README.md; asserted here.
    for path in ROLES:
        assert "Agent" not in tools_of(path), f"{path.name} can spawn agents"


@pytest.mark.parametrize("name", GOVERNING)
def test_the_governing_document_still_exists(name):
    # If one is renamed, the sentences above start naming a file that is not
    # there, and the rule quietly stops describing anything.
    root = AGENTS.parents[1]
    assert (root / name).is_file(), f"{name} is named as governing but does not exist"
