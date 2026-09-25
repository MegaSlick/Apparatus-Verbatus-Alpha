"""Tests for tidy.py, a report that changes nothing.

Several tests carry a positive control, a file that must appear in the report: an
assertion of absence is satisfied just as well by a run that never looked.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent


def load_tidy(tmp_path):
    spec = importlib.util.spec_from_file_location("tidy_under_test", HOOKS / "tidy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = tmp_path / "repo"
    wb = repo / "workbench"
    mod.REPO = repo
    mod.WORKBENCH = wb
    mod.ACTIVE = wb / "active"
    mod.STANDING = wb / "standing"
    mod.DESIGN = wb / "design"
    mod.ARCHIVE = wb / "archive"
    mod.SCRATCH = wb / "scratch"
    mod.RAW = wb / "raw"
    # Every drawer points at the fixture, never at this machine's real tree.
    mod.QUARANTINE = wb / "quarantine"
    mod.MEMORY = tmp_path / "memory"
    for d in (
        mod.ACTIVE,
        mod.STANDING,
        mod.DESIGN,
        mod.ARCHIVE,
        mod.SCRATCH,
        mod.RAW,
        mod.QUARANTINE,
    ):
        d.mkdir(parents=True)
    return mod


def test_a_large_archived_file_is_never_hashed_for_the_duplicate_check(tmp_path, capsys):
    """Anything above ARCHIVE_DIGEST_MAX_BYTES is never hashed; the small pair is the
    positive control."""
    tidy = load_tidy(tmp_path)
    big = b"b" * (tidy.ARCHIVE_DIGEST_MAX_BYTES + 1)
    (tidy.ARCHIVE / "bundle.bin").write_bytes(big)
    (tidy.ACTIVE / "copy.bin").write_bytes(big)
    (tidy.ARCHIVE / "old.md").write_text("same bytes")
    (tidy.ACTIVE / "note.md").write_text("same bytes")

    tidy.main([])
    out = capsys.readouterr().out
    reported = out.split("already archived")[1].split("\n\n")[0]
    assert "note.md" in reported, out
    assert "copy.bin" not in reported, "a file above the cap must not be hashed or matched"


def test_duplicate_is_reported_and_nothing_moves(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "old.md").write_text("same bytes")
    (tidy.ACTIVE / "note.md").write_text("same bytes")
    (tidy.ACTIVE / "live.md").write_text("different")

    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "already archived" in out
    assert "note.md" in out
    assert (tidy.ACTIVE / "note.md").exists(), "the report must leave active/ alone"
    assert not any(tidy.SCRATCH.iterdir()), "tidy.py no longer writes anywhere"


def test_the_retired_file_flag_is_refused(tmp_path):
    # A caller still passing --file believes a move happened; accepting it would agree.
    tidy = load_tidy(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        tidy.main(["--file"])
    assert exit_info.value.code == 2


def test_handoff_is_never_reported_as_a_duplicate(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "2026-01-01_x").mkdir()
    (tidy.ARCHIVE / "2026-01-01_x" / "HANDOFF.md").write_text("identical")
    (tidy.ACTIVE / "HANDOFF.md").write_text("identical")
    # The brief is archived in the same copy-then-overwrite step as the handoff.
    (tidy.ARCHIVE / "2026-01-01_x" / "NEXT_SESSION_BRIEF.md").write_text("identical brief")
    (tidy.ACTIVE / "NEXT_SESSION_BRIEF.md").write_text("identical brief")
    # Positive controls, each with its own content so neither is named as the other's match.
    (tidy.ARCHIVE / "2026-01-01_x" / "note.md").write_text("also identical")
    (tidy.ACTIVE / "note.md").write_text("also identical")
    (tidy.ARCHIVE / "2026-01-01_x" / "queue.md").write_text("a third duplicate")
    (tidy.ACTIVE / "queue.md").write_text("a third duplicate")

    tidy.main([])

    reported = capsys.readouterr().out.split("already archived")[1]
    assert "note.md" in reported, "the duplicate scan did not run"
    assert "queue.md" in reported, "the brief's positive control did not run"
    assert "HANDOFF.md" not in reported, "the live handoff must never be reported as redundant"
    assert "NEXT_SESSION_BRIEF.md" not in reported, (
        "the live brief must never be reported as redundant"
    )


def test_design_is_never_reported(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "a.md").write_text("dup")
    (tidy.DESIGN / "a.md").write_text("dup")
    # The same bytes in active/ must be reported, so design/'s absence from the
    # duplicate list is an exemption rather than a run that did nothing.
    (tidy.ACTIVE / "a.md").write_text("dup")
    (tidy.ACTIVE / "HANDOFF.md").write_text("live")

    tidy.main([])

    out = capsys.readouterr().out
    duplicates = out.split("already archived")[1].split("\n\n")[0]
    assert "active/a.md" in duplicates, "the duplicate scan did not run"
    assert "design/a.md" not in duplicates, "design/ is not tidy.py's to judge"
    assert (tidy.DESIGN / "a.md").exists()


def test_empty_active_still_audits_memory(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    tidy.MEMORY.mkdir(parents=True)
    (tidy.MEMORY / "MEMORY.md").write_text("- [Gone](gone.md) — dangling\n")
    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "empty — no handoff" in out
    assert "missing file: gone.md" in out, "the audit must not stop at active/"


def test_a_clean_drawer_reports_nothing_and_exits_zero(tmp_path, capsys):
    """Callers branch on a three-valued contract; this pins the 0 arm."""
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    # Clean now includes a readable suspension ledger, so the fixture carries one.
    (tidy.STANDING / "SUSPENSIONS.md").write_text("none in force\n")
    assert tidy.main([]) == 0
    assert "nothing wants attention" in capsys.readouterr().out


def test_a_missing_workbench_is_a_failure_not_a_pass(tmp_path, capsys):
    """And the 2 arm: a report that could not look must not read as clean."""
    tidy = load_tidy(tmp_path)
    tidy.WORKBENCH = tmp_path / "absent"
    assert tidy.main([]) == 2
    assert "workbench/ not found" in capsys.readouterr().err


def test_an_index_link_to_a_non_markdown_file_that_exists_is_not_dangling(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    (tidy.STANDING / "SUSPENSIONS.md").write_text("none in force\n")
    tidy.MEMORY.mkdir(parents=True)
    (tidy.MEMORY / "log.txt").write_text("kept\n")
    (tidy.MEMORY / "MEMORY.md").write_text("- [Log](log.txt) — exists, just not markdown\n")
    assert tidy.main([]) == 0, "an existing target is unusual, not missing"
    assert "missing file" not in capsys.readouterr().out


def test_memory_links_survive_markdown_shapes(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    tidy.MEMORY.mkdir(parents=True)
    (tidy.MEMORY / "real.md").write_text("x")
    (tidy.MEMORY / "spaced name.md").write_text("x")
    (tidy.MEMORY / "MEMORY.md").write_text(
        "- [Real (v2)](./real.md#hook) — parens and prefix\n"
        "- [Spaced](<spaced name.md>) — an angle-bracketed target with a space\n"
        "- [Gone](vanished.md) — a genuinely dangling target\n"
    )
    (tidy.ACTIVE / "HANDOFF.md").write_text("live")

    tidy.main([])

    out = capsys.readouterr().out
    # Positive control: the memory audit must actually be reporting.
    assert "missing file: vanished.md" in out, "the memory audit did not run"
    assert "missing file: real.md" not in out, "normalised link shapes must resolve"
    assert "missing file: spaced name.md" not in out, "<angle-bracketed> targets may hold spaces"
    assert "file is in no index line" not in out, "every present memory is linked"


def test_an_over_full_active_is_reported_against_the_budget(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    for i in range(tidy.ACTIVE_FILE_BUDGET + 1):
        (tidy.ACTIVE / f"n{i}.md").write_text(f"note {i}")

    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "past the one-sitting budget" in out
    assert f"{tidy.ACTIVE_FILE_BUDGET + 1}/{tidy.ACTIVE_FILE_BUDGET} files" in out


def test_standing_ledgers_are_listed_but_never_budgeted_or_filed(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    (tidy.ARCHIVE / "twin.md").write_text("same bytes")
    (tidy.STANDING / "SUSPENSIONS.md").write_text("same bytes")
    (tidy.STANDING / "MASTER_PLAN.md").write_text("the plan")

    assert tidy.main([]) == 0, "standing ledgers alone must not want attention"
    out = capsys.readouterr().out
    assert "standing/ 2 ledgers" in out
    assert "SUSPENSIONS.md" in out
    assert "already archived" not in out, (
        "a standing ledger is never a filing candidate, however identical"
    )
    assert "past the one-sitting budget" not in out


def test_a_missing_standing_drawer_is_reported_loudly(tmp_path, capsys):
    # A missing drawer used to print nothing, which reads like no suspension in force.
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    shutil.rmtree(tidy.STANDING)

    assert tidy.main([]) == 1, "a ledger that cannot be read is not a clean report"
    out = capsys.readouterr().out
    assert "standing/ MISSING" in out
    assert "SUSPENSIONS.md" in out, "the report must name what is unreadable"


def test_an_unreadable_suspension_ledger_is_reported_not_passed(tmp_path, capsys):
    # is_file() is true for a ledger this process cannot open: a stat is not a read.
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    ledger = tidy.STANDING / "SUSPENSIONS.md"
    ledger.write_text("none in force\n")
    ledger.chmod(0)
    try:
        try:
            ledger.read_text()
        except OSError:
            pass
        else:
            pytest.skip("permissions are not enforced here (running as root?)")
        assert tidy.main([]) == 1, "an unreadable ledger is not a clean report"
        out = capsys.readouterr().out
        assert "cannot be read" in out
        assert "SUSPENSIONS.md" in out
    finally:
        ledger.chmod(0o600)


def test_a_standing_drawer_without_the_suspension_ledger_is_reported(tmp_path, capsys):
    # Present-but-empty is the same unknown as absent: nothing here can tell a
    # session that no suspension is in force, only that nobody wrote one down.
    tidy = load_tidy(tmp_path)
    (tidy.ACTIVE / "HANDOFF.md").write_text("live\n")
    (tidy.STANDING / "MASTER_PLAN.md").write_text("the plan")

    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "SUSPENSIONS.md" in out
    # The positive control: the drawer was read, so the finding is an absence
    # rather than a listing that never ran.
    assert "MASTER_PLAN.md" in out
