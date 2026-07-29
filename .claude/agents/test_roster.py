"""The roster's own claims, asserted.

Hard rule 10 — a spawned agent never edits the governing documents — is stated in
every writing role and has a tripwire in the Claude-side guard. Neither mechanism
turns the rule into a security boundary, so a role must still carry both the
prohibition and the instruction to propose or report the change instead.

These tests inspect that dedicated policy bullet. Looking for either word anywhere
in a role let unrelated prose satisfy the rule while the actual instruction was
missing.
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


def governing_policy(path):
    """Return the one list item that tells a writing role how to handle governing docs."""
    blocks = []
    current = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- "):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            if line.startswith("## "):
                blocks.append("\n".join(current))
                current = []
            else:
                current.append(line)
    if current:
        blocks.append("\n".join(current))

    policies = [
        block
        for block in blocks
        if re.search(r"\b(?:governing|canonical) documents?\b", block, re.I)
        and re.search(r"\b(?:never edit|do not touch)\b", block, re.I)
    ]
    assert len(policies) == 1, (
        f"{path.name} must carry exactly one governing-document policy bullet; "
        f"found {len(policies)}"
    )
    return policies[0]


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

    policy = governing_policy(path)
    assert re.search(r"\b(?:never edit|do not touch)\b", policy, re.I), (
        f"{path.name} can Write and Edit but never tells the agent to leave the "
        "governing documents alone in its policy block."
    )


@pytest.mark.parametrize("path", ROLES, ids=lambda p: p.stem)
def test_a_writing_role_says_to_propose_rather_than_amend(path):
    # Half the rule is "do not edit". The other half is what to do instead, and
    # an agent given only the prohibition tends to route around it — silently
    # dropping the change, or making it somewhere adjacent.
    if not ({"Write", "Edit"} & tools_of(path)):
        pytest.skip(f"{path.stem} holds no write tools")
    policy = governing_policy(path)
    assert re.search(r"\b(?:propos\w*|report\w*)\b", policy, re.I), (
        f"{path.name} forbids editing the governing documents but never says to "
        "propose or report the change in the same policy block"
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
