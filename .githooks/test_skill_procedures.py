"""Execute the shell the skills tell a session to run.

`operations/codex/capture-seat-report.sh` produces the evidence a push decision
rests on. When it was prose in `reviewer-pass/SKILL.md`, nothing ran: every failure
mode inside it was unobserved, and the five guards it carries could be reworded away
without anything noticing. These tests then lifted the fenced block back out of the
Markdown to run it — which worked, and meant the executable half of a procedure was
stored as documentation.

It is now a file, and these run that file against fakes. A guard edited into
something that does not work fails here; a rewording of the skill page does not.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAPTURE = ROOT / "operations" / "codex" / "capture-seat-report.sh"

FAKE_SEAT = """\
#!/bin/sh
printf 'finding one\\nfinding two\\n'
"""

# The subject here is the script's file handling, not the scanner: a real
# check_ingress.py run would make this test fail whenever that file is
# mid-edit, which is a different thing being tested.
FAKE_SCAN = "import sys\nsys.stdin.buffer.read()\nraise SystemExit(0)\n"


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    (root / "operations" / "codex").mkdir(parents=True)
    (root / ".githooks").mkdir(parents=True)
    seat = root / "operations" / "codex" / "seat.sh"
    seat.write_text(FAKE_SEAT, encoding="utf-8")
    seat.chmod(0o755)
    # The real script under test, in the place it expects to be run from — the
    # copy is what lets seat.sh and check_ingress.py be fakes at the same relative
    # paths without touching the repository's own.
    (root / "operations" / "codex" / CAPTURE.name).write_text(
        CAPTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / ".githooks" / "check_ingress.py").write_text(FAKE_SCAN, encoding="utf-8")
    (root / "prompt.txt").write_text("review this\n", encoding="utf-8")
    (root / "reports").mkdir()
    return root


def run_capture(root: Path, *arguments: str):
    if not arguments:
        arguments = ("audit-sol", "prompt.txt", "reports/gpt-sol.log")
    return subprocess.run(
        ["sh", f"operations/codex/{CAPTURE.name}", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "POSIXLY_CORRECT": "1"},
    )


def test_it_files_a_clean_report(tmp_path):
    root = workspace(tmp_path)
    result = run_capture(root)

    assert result.returncode == 0, result.stdout + result.stderr
    report = root / "reports" / "gpt-sol.log"
    assert report.read_text(encoding="utf-8") == "finding one\nfinding two\n"
    assert list((root / "reports").iterdir()) == [report], "a temporary file was left behind"


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("audit-sol",),
        ("audit-sol", "prompt.txt"),
        ("audit-sol", "prompt.txt", "reports/a.log", "extra"),
    ],
)
def test_it_refuses_the_wrong_number_of_arguments(tmp_path, arguments):
    # Exit 2, not 1: a caller that got the call wrong has not had a review fail,
    # and the two must not read the same to whatever is checking.
    root = workspace(tmp_path)
    result = subprocess.run(
        ["sh", f"operations/codex/{CAPTURE.name}", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_it_refuses_a_missing_prompt_file(tmp_path):
    root = workspace(tmp_path)
    result = run_capture(root, "audit-sol", "absent.txt", "reports/gpt-sol.log")
    assert result.returncode == 2
    assert "no prompt file" in result.stderr
    assert list((root / "reports").iterdir()) == []


def test_it_refuses_a_missing_report_directory(tmp_path):
    root = workspace(tmp_path)
    result = run_capture(root, "audit-sol", "prompt.txt", "absent/gpt-sol.log")
    assert result.returncode == 2
    assert "no report directory" in result.stderr


def test_it_refuses_to_write_through_a_dangling_symlink(tmp_path):
    """`[ ! -e ]` follows symlinks, so a dangling link reads as "nothing there"."""
    root = workspace(tmp_path)
    victim = root / "earlier-evidence.log"
    (root / "reports" / "gpt-sol.log").symlink_to(victim)

    result = run_capture(root)

    assert not victim.exists(), "the script wrote through a symlink onto another path"
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing to overwrite evidence" in result.stderr


def test_it_refuses_when_the_report_already_exists(tmp_path):
    root = workspace(tmp_path)
    existing = root / "reports" / "gpt-sol.log"
    existing.write_text("an earlier reviewer's report\n", encoding="utf-8")

    result = run_capture(root)

    assert result.returncode == 1
    assert existing.read_text(encoding="utf-8") == "an earlier reviewer's report\n"


def test_it_keeps_the_evidence_when_the_seat_fails(tmp_path):
    root = workspace(tmp_path)
    seat = root / "operations" / "codex" / "seat.sh"
    seat.write_text("#!/bin/sh\nprintf 'partial finding\\n'\nexit 3\n", encoding="utf-8")

    result = run_capture(root)

    assert result.returncode == 1
    assert (root / "reports" / "gpt-sol.log").read_text(encoding="utf-8") == "partial finding\n"
    assert "failed with exit 3" in result.stderr


def test_it_refuses_an_empty_report(tmp_path):
    root = workspace(tmp_path)
    (root / "operations" / "codex" / "seat.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result = run_capture(root)

    assert result.returncode == 1
    assert "empty report" in result.stderr
    assert list((root / "reports").iterdir()) == []


def test_it_writes_nothing_when_the_scan_refuses(tmp_path):
    root = workspace(tmp_path)
    (root / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.stdin.buffer.read()\nraise SystemExit(1)\n", encoding="utf-8"
    )

    result = run_capture(root)

    assert result.returncode == 1
    assert not (root / "reports" / "gpt-sol.log").exists()
    assert list((root / "reports").iterdir()) == [], "a temporary file survived a failed scan"


def trap_lines() -> list[str]:
    return [
        line.strip()
        for line in CAPTURE.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("trap ") and "- EXIT" not in line
    ]


@pytest.mark.parametrize("signal_name", ["HUP", "INT", "TERM"])
def test_the_cleanup_trap_stops_the_procedure_it_cleans_up_after(tmp_path, signal_name):
    """A trap that tidies up and returns lets the next step run anyway.

    The remaining steps write the report and then judge the seat's exit status.
    Resuming after a signal means writing evidence from a call that was
    interrupted, and the push decision would then rest on it.
    """
    root = workspace(tmp_path)
    traps = "\n".join(trap_lines())
    assert traps, "the script installs no cleanup trap"

    script = (
        "set -eu\n"
        'temporary=$(mktemp "$PWD/reports/.capture-seat-report.XXXXXX")\n'
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


def test_the_skill_calls_the_script_rather_than_carrying_a_copy(tmp_path):
    # The point of the extraction: the procedure lives in one place. A fenced
    # reimplementation in the skill would drift from the file these tests cover.
    skill = (ROOT / ".claude" / "skills" / "reviewer-pass" / "SKILL.md").read_text(encoding="utf-8")
    assert "capture-seat-report.sh" in skill, "the skill no longer invokes the capture script"
    assert "mktemp" not in skill, "the skill has grown its own copy of the capture procedure"


def test_it_refuses_a_target_that_appears_after_the_check(tmp_path):
    """The check-then-publish race two reviewers found.

    The existence check and the publish are separate steps. `mv` would replace a
    file created in between; a hard link fails instead, which is what makes the two
    steps one decision. Simulated by making the seat itself create the target while
    the script is mid-flight — the same interleaving, deterministically.
    """
    root = workspace(tmp_path)
    (root / "operations" / "codex" / "seat.sh").write_text(
        "#!/bin/sh\nprintf 'other findings\\n' > \"$PWD/reports/gpt-sol.log\"\n"
        "printf 'my findings\\n'\n",
        encoding="utf-8",
    )

    result = run_capture(root)

    assert result.returncode == 1
    assert "refusing to overwrite evidence" in result.stderr
    assert (root / "reports" / "gpt-sol.log").read_text(encoding="utf-8") == "other findings\n"
    assert [p.name for p in (root / "reports").iterdir()] == ["gpt-sol.log"], (
        "a temporary file survived the refused publish"
    )


def test_a_seat_that_never_ran_is_not_published_as_a_report(tmp_path):
    """`seat.sh` exit 2 is its own usage/precondition status, not a review.

    Its stderr is merged into the captured output, so a one-line complaint like
    "no seat named audit-xyz" was non-empty, passed the scan, and got filed as the
    reviewer's report — after which the refusal-to-overwrite guard blocked the
    corrected re-run. Reproduced by a review before this was fixed.
    """
    root = workspace(tmp_path)
    (root / "operations" / "codex" / "seat.sh").write_text(
        '#!/bin/sh\necho "seat: no seat named audit-xyz" >&2\nexit 2\n', encoding="utf-8"
    )

    result = run_capture(root, "audit-xyz", "prompt.txt", "reports/gpt-sol.log")

    assert result.returncode == 2, "a seat that never ran must not read as a failed review"
    assert "did not run" in result.stderr
    assert not (root / "reports" / "gpt-sol.log").exists()
    assert list((root / "reports").iterdir()) == [], "a temporary file survived"


def test_a_seat_that_ran_and_then_failed_still_keeps_its_evidence(tmp_path):
    # The counterpart, and the reason exit 2 had to be singled out rather than all
    # non-zero statuses: a reviewer that said something real and then crashed must
    # not lose it. Exit 3 is not seat.sh's usage status.
    root = workspace(tmp_path)
    (root / "operations" / "codex" / "seat.sh").write_text(
        "#!/bin/sh\nprintf 'partial finding\\n'\nexit 3\n", encoding="utf-8"
    )

    result = run_capture(root)

    assert result.returncode == 1
    assert (root / "reports" / "gpt-sol.log").read_text(encoding="utf-8") == "partial finding\n"


@pytest.mark.parametrize("scan_exit", [2, 127])
def test_a_scan_that_could_not_run_is_not_reported_as_a_finding(tmp_path, scan_exit):
    """check_ingress.py answers 1 for "found one" and 2 for "could not decide".

    Collapsing them told the operator a credential had been found when nothing had
    been measured — GOVERNANCE 10 — and discarded a report that may have cost a
    45-minute paid run. `commit-msg` and `applypatch-msg` already distinguish them.
    """
    root = workspace(tmp_path)
    (root / ".githooks" / "check_ingress.py").write_text(
        f"import sys\nsys.stdin.buffer.read()\nraise SystemExit({scan_exit})\n", encoding="utf-8"
    )

    result = run_capture(root)

    assert result.returncode == 2
    assert "could not run" in result.stderr
    assert "not a finding about its contents" in result.stderr
    assert not (root / "reports" / "gpt-sol.log").exists()


def test_a_real_credential_finding_still_says_so(tmp_path):
    root = workspace(tmp_path)
    (root / ".githooks" / "check_ingress.py").write_text(
        "import sys\nsys.stdin.buffer.read()\nraise SystemExit(1)\n", encoding="utf-8"
    )

    result = run_capture(root)

    assert result.returncode == 1
    assert "failed credential scanning" in result.stderr
    assert not (root / "reports" / "gpt-sol.log").exists()


def test_a_seat_that_ran_and_exited_two_keeps_its_findings(tmp_path):
    """The collision a reviewer found at the commit that introduced the exit-2 rule.

    `seat.sh` reserves exit 2 for its own preconditions — an unknown seat, no
    `timeout` on PATH — and `capture-seat-report.sh` reads 2 as "never ran" and writes
    nothing. But seat.sh also passed a *child's* status straight through, so a Codex
    run that produced a finding and happened to exit 2 was announced as never having
    run and its report discarded. GOVERNANCE 2 and 10 both bite: a finding lost, and a
    claim made about something nobody established. seat.sh now reports a child's 2 as
    3, keeping the original in text.
    """
    root = workspace(tmp_path)
    # The real seat.sh, so this exercises the wrapper's own remapping rather than a
    # fake standing in for it. It needs a seats table and a stub `codex` on PATH.
    real_seat = (ROOT / "operations" / "codex" / "seat.sh").read_text(encoding="utf-8")
    (root / "operations" / "codex" / "seat.sh").write_text(real_seat, encoding="utf-8")
    (root / "operations" / "codex" / "seats.conf").write_text(
        "audit-sol   gpt-5.6-sol         high 2700\n", encoding="utf-8"
    )
    stub = root / "bin"
    stub.mkdir()
    # seat.sh refuses to run uncapped; supply the deadline wrapper it looks for
    # so this test does not depend on coreutils being installed on the host.
    (stub / "timeout").write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    (stub / "timeout").chmod(0o755)
    # A child that RAN, said something real, and chose exit 2 for its own reasons.
    (stub / "codex").write_text(
        "#!/bin/sh\nprintf 'finding: a real defect\\n'\nexit 2\n", encoding="utf-8"
    )
    (stub / "codex").chmod(0o755)

    result = subprocess.run(
        ["sh", f"operations/codex/{CAPTURE.name}", "audit-sol", "prompt.txt", "reports/r.log"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
    )

    report = root / "reports" / "r.log"
    assert report.exists(), "a seat that ran and exited 2 lost its report"
    assert "finding: a real defect" in report.read_text(encoding="utf-8")
    assert "did not run" not in result.stderr, "a seat that ran was announced as never running"
    assert result.returncode == 1, "a failed-but-productive seat reports failure, not absence"
