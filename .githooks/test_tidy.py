"""Tests for tidy.py — a report that changes nothing.

Each test builds a disposable workbench and points the module at it. What is
proven: a byte-identical duplicate is reported and left where it is; the retired
`--file` flag is refused rather than silently accepted; HANDOFF.md is never
reported as redundant however identical it looks; design/ is not tidy.py's to
judge; an empty active/ still audits project memory, so the report does not stop
at the missing handoff; markdown link shapes resolve; and an over-full active/ is
reported against the one-sitting budget.

Several tests carry a positive control — a second file that *must* appear in the
report — because an assertion that something is absent is satisfied just as well
by a run that never looked.
"""

import importlib.util
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
    mod.DESIGN = wb / "design"
    mod.ARCHIVE = wb / "archive"
    mod.SCRATCH = wb / "scratch"
    mod.RAW = wb / "raw"
    mod.MEMORY = tmp_path / "memory"
    for d in (mod.ACTIVE, mod.DESIGN, mod.ARCHIVE, mod.SCRATCH, mod.RAW):
        d.mkdir(parents=True)
    return mod


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
    # A caller that still passes --file believed a move happened. Accepting the
    # flag and reporting instead would let it go on believing that.
    tidy = load_tidy(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        tidy.main(["--file"])
    assert exit_info.value.code == 2


def test_handoff_is_never_reported_as_a_duplicate(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "2026-01-01_x").mkdir()
    (tidy.ARCHIVE / "2026-01-01_x" / "HANDOFF.md").write_text("identical")
    (tidy.ACTIVE / "HANDOFF.md").write_text("identical")
    # The positive control: a second duplicate that must be reported. Without it
    # this passes against a tidy.py whose duplicate scan does nothing at all.
    (tidy.ARCHIVE / "2026-01-01_x" / "note.md").write_text("also identical")
    (tidy.ACTIVE / "note.md").write_text("also identical")

    tidy.main([])

    out = capsys.readouterr().out
    assert "note.md" in out, "the duplicate scan did not run"
    assert "HANDOFF.md" not in out.split("already archived")[1], (
        "the live handoff must never be reported as redundant"
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
    # The positive control: the audit must actually be reporting, or "no missing
    # file" below would be satisfied by a run that never looked.
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
