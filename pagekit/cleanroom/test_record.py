"""The clean-room record holds: no HOLD, decided incidents, an append-only log, true briefs."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from pagekit.cleanroom.gate import has_decision, incidents_kept, replay_hold

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HOLD = HERE / "HOLD"
LOG = HERE / "LOG.md"
LOG_PATH = "pagekit/cleanroom/LOG.md"
MARKER = b"----- brief below this line, exactly as sent -----\n"
ENTRY = re.compile(r"^## (\d{4}) ", re.MULTILINE)


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)


def test_pagekit_is_not_on_hold():
    assert not HOLD.exists(), (
        "pagekit is on HOLD while a suspected leak is investigated; nothing merges until "
        "the lead decides and HOLD is removed (pagekit/cleanroom/CLEANROOM.md)"
    )


def test_every_incident_note_carries_the_leads_decision():
    if HOLD.exists():
        pytest.skip("HOLD is in place, so the open incident is still undecided")
    for note in sorted((HERE / "incidents").glob("*.md")):
        assert has_decision(note.read_text(encoding="utf-8")), f"{note.name} has no Decision line"


def test_the_incident_template_leaves_the_decision_to_the_lead():
    text = (HERE / "templates" / "incident.md").read_text(encoding="utf-8")
    assert "\nDecision:\n" in text
    assert not has_decision(text)
    for choice in ("Purge", "Minor breach", "False flag"):
        assert f"*{choice}:*" in text


def require_origin_main() -> None:
    if git("rev-parse", "--verify", "--quiet", "origin/main^{commit}").returncode != 0:
        pytest.fail("origin/main is not available, so the record cannot be compared; fetch it")


def test_every_commit_this_branch_adds_keeps_the_hold_rule():
    """A hook skipped with --no-verify cannot lift a HOLD or erase an incident unseen."""
    require_origin_main()
    assert replay_hold(ROOT, "origin/main") == []


def test_incident_notes_already_on_main_are_kept():
    require_origin_main()
    assert incidents_kept(ROOT, "origin/main") == []


def test_every_saved_finding_is_named_in_the_log_by_its_digest():
    log = LOG.read_text(encoding="utf-8")
    for finding in sorted((HERE / "findings").glob("*")):
        digest = hashlib.sha256(finding.read_bytes()).hexdigest()
        assert digest in log, f"{finding.name}: its sha256 is not in LOG.md"


def test_log_entries_already_on_main_are_kept_byte_for_byte():
    """main's LOG.md must be a prefix of this tree's: entries are added, never changed.

    CI checks out full history, so origin/main is there. Compared with the current main,
    not the merge base, so a branch must merge main before its log can pass.
    """
    require_origin_main()
    if git("cat-file", "-e", f"origin/main:{LOG_PATH}").returncode != 0:
        pytest.skip("LOG.md is not on origin/main yet, so there are no entries it must keep")
    on_main = git("cat-file", "blob", f"origin/main:{LOG_PATH}")
    assert on_main.returncode == 0
    assert LOG.read_bytes().startswith(on_main.stdout), (
        "LOG.md changed an entry that is already on main; add a new entry instead"
    )


def test_log_entries_are_numbered_in_order():
    numbers = [int(number) for number in ENTRY.findall(LOG.read_text(encoding="utf-8"))]
    assert numbers == list(range(1, len(numbers) + 1))


def test_every_saved_brief_matches_its_logged_digest():
    log = LOG.read_text(encoding="utf-8")
    briefs = sorted((HERE / "briefs").glob("*.md"))
    assert briefs
    for brief in briefs:
        header, marker, body = brief.read_bytes().partition(MARKER)
        assert marker, f"{brief.name} has no marker line"
        digest = hashlib.sha256(body).hexdigest()
        assert digest in header.decode("utf-8"), f"{brief.name}: body does not match its header"
        assert digest in log, f"{brief.name}: its sha256 is not in LOG.md"
