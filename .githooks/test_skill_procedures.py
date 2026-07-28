"""Execute the shell the skills tell a session to run.

The session-start, session-end and reviewer-pass skills are executable
procedures, and the reviewer-pass snippet is the one that produces the
evidence a push receipt rests on. Until now a single test asserted three
literal strings from it — nothing ran, so every failure mode inside it was
unobserved (ledger L45).

These tests lift the fenced blocks straight out of the Markdown and run them
against fakes. A snippet that is edited into something that does not work
fails here; a snippet that is only reworded does not.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REVIEWER_PASS = ROOT / ".claude" / "skills" / "reviewer-pass" / "SKILL.md"

FAKE_SEAT = """\
#!/bin/sh
printf 'finding one\\nfinding two\\n'
"""

# The subject here is the snippet's file handling, not the scanner: a real
# check_ingress.py run would make this test fail whenever that file is
# mid-edit, which is a different thing being tested.
FAKE_SCAN = "import sys\nsys.stdin.buffer.read()\nraise SystemExit(0)\n"


def fenced_blocks(document: Path, language: str = "sh") -> list[str]:
    text = document.read_text(encoding="utf-8")
    blocks = re.findall(rf"(?ms)^\s*```{language}\n(.*?)^\s*```", text)
    return [textwrap.dedent(block) for block in blocks]


def gpt_capture_snippet() -> str:
    """The reviewer-pass block that captures, scans and files the GPT report."""
    for block in fenced_blocks(REVIEWER_PASS):
        if "seat.sh judge" in block:
            return block
    raise AssertionError("the reviewer-pass skill no longer contains the GPT capture snippet")


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "operations" / "codex").mkdir(parents=True)
    (root / ".githooks").mkdir(parents=True)
    seat = root / "operations" / "codex" / "seat.sh"
    seat.write_text(FAKE_SEAT, encoding="utf-8")
    seat.chmod(0o755)
    (root / ".githooks" / "check_ingress.py").write_text(FAKE_SCAN, encoding="utf-8")
    (root / "prompt.txt").write_text("review this\n", encoding="utf-8")
    (root / "reports").mkdir()
    return root


def run_snippet(root: Path, snippet: str, *, preamble: str = "", timeout: int = 30):
    script = f'report_dir="$PWD/reports"\nprompt_path="$PWD/prompt.txt"\n{preamble}{snippet}'
    return subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "POSIXLY_CORRECT": "1"},
    )


def test_the_gpt_capture_snippet_files_a_clean_report(tmp_path):
    root = workspace(tmp_path)
    result = run_snippet(root, gpt_capture_snippet())

    assert result.returncode == 0, result.stdout + result.stderr
    report = root / "reports" / "gpt-sol.log"
    assert report.read_text(encoding="utf-8") == "finding one\nfinding two\n"
    assert list((root / "reports").iterdir()) == [report], "a temporary file was left behind"


def test_the_snippet_refuses_to_write_through_a_dangling_symlink(tmp_path):
    """`[ ! -e ]` follows symlinks, so a dangling link reads as "nothing there"."""
    root = workspace(tmp_path)
    victim = root / "earlier-evidence.log"
    (root / "reports" / "gpt-sol.log").symlink_to(victim)

    result = run_snippet(root, gpt_capture_snippet())

    assert not victim.exists(), "the snippet wrote through a symlink onto another path"
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to overwrite evidence" in result.stderr


def test_the_snippet_refuses_when_the_report_already_exists(tmp_path):
    root = workspace(tmp_path)
    existing = root / "reports" / "gpt-sol.log"
    existing.write_text("an earlier reviewer's report\n", encoding="utf-8")

    result = run_snippet(root, gpt_capture_snippet())

    assert result.returncode == 1
    assert existing.read_text(encoding="utf-8") == "an earlier reviewer's report\n"


def test_the_snippet_keeps_the_evidence_when_the_seat_fails(tmp_path):
    root = workspace(tmp_path)
    seat = root / "operations" / "codex" / "seat.sh"
    seat.write_text("#!/bin/sh\nprintf 'partial finding\\n'\nexit 3\n", encoding="utf-8")

    result = run_snippet(root, gpt_capture_snippet())

    assert result.returncode == 1
    assert (root / "reports" / "gpt-sol.log").read_text(encoding="utf-8") == "partial finding\n"
    assert "failed with exit 3" in result.stderr


def test_the_snippet_writes_nothing_when_the_scan_refuses(tmp_path):
    root = workspace(tmp_path)
    (root / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.stdin.buffer.read()\nraise SystemExit(1)\n", encoding="utf-8"
    )

    result = run_snippet(root, gpt_capture_snippet())

    assert result.returncode == 1
    assert not (root / "reports" / "gpt-sol.log").exists()
    assert list((root / "reports").iterdir()) == [], "a temporary file survived a failed scan"


def trap_lines() -> list[str]:
    return [
        line.strip()
        for line in gpt_capture_snippet().splitlines()
        if line.strip().startswith("trap ") and "- EXIT" not in line
    ]


@pytest.mark.parametrize("signal_name", ["HUP", "INT", "TERM"])
def test_the_cleanup_trap_stops_the_procedure_it_cleans_up_after(tmp_path, signal_name):
    """A trap that tidies up and returns lets the next step run anyway.

    The snippet's remaining steps write the report and then judge the seat's
    exit status. Resuming after a signal means writing evidence from a call
    that was interrupted, and the receipt would then rest on it.
    """
    root = workspace(tmp_path)
    traps = "\n".join(trap_lines())
    assert traps, "the snippet installs no cleanup trap"

    script = (
        "set -eu\n"
        'gpt_temporary=$(mktemp "$PWD/reports/.gpt-sol.log.XXXXXX")\n'
        f"{traps}\n"
        f"kill -{signal_name} $$\n"
        'echo resumed > "$PWD/resumed"\n'
    )
    result = subprocess.run(
        ["sh", "-c", script], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )

    assert not (root / "resumed").exists(), (
        f"the procedure carried on after {signal_name}, having announced cleanup"
    )
    assert result.returncode != 0, f"an interrupted procedure reported success after {signal_name}"
    assert list((root / "reports").iterdir()) == [], "the temporary file survived the signal"
