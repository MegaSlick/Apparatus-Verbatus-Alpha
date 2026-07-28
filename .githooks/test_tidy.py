"""Tests for tidy.py — the one script in the harness that moves files.

Each test builds a disposable workbench and points the module at it. What is
proven: a byte-identical duplicate is reported and, with --file, moved to
scratch/ rather than deleted; HANDOFF.md is never moved however identical;
design/ is never touched; an empty active/ still audits project memory (the
report must not stop at the missing handoff); and the one-sitting budget is
reported on the drawer as the run leaves it, not as it found it.
"""

import importlib.util
from pathlib import Path

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


def test_duplicate_reported_then_moved_only_with_file(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "old.md").write_text("same bytes")
    (tidy.ACTIVE / "note.md").write_text("same bytes")
    (tidy.ACTIVE / "live.md").write_text("different")

    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "already archived" in out
    assert (tidy.ACTIVE / "note.md").exists(), "report-only run must move nothing"

    tidy.main(["--file"])
    assert not (tidy.ACTIVE / "note.md").exists()
    assert (tidy.SCRATCH / "note.md").exists(), "moved to scratch, never deleted"


def test_duplicate_changed_after_scan_is_not_moved(tmp_path, capsys, monkeypatch):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "old.md").write_text("same bytes")
    active = tidy.ACTIVE / "note.md"
    active.write_text("same bytes")

    real_digest = tidy.digest
    active_reads = 0

    def changing_digest(path):
        nonlocal active_reads
        if path == active:
            active_reads += 1
            if active_reads == 2:
                active.write_text("new work")
        return real_digest(path)

    monkeypatch.setattr(tidy, "digest", changing_digest)
    assert tidy.main(["--file"]) == 1
    out = capsys.readouterr().out
    assert "changed after the duplicate scan" in out
    assert active.read_text() == "new work"
    assert not (tidy.SCRATCH / "note.md").exists()


def test_handoff_is_never_filed_as_a_duplicate(tmp_path):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "2026-01-01_x").mkdir()
    (tidy.ARCHIVE / "2026-01-01_x" / "HANDOFF.md").write_text("identical")
    (tidy.ACTIVE / "HANDOFF.md").write_text("identical")
    tidy.main(["--file"])
    assert (tidy.ACTIVE / "HANDOFF.md").exists(), "the live handoff must never move"
    assert not (tidy.SCRATCH / "HANDOFF.md").exists()


def test_design_is_never_touched(tmp_path):
    tidy = load_tidy(tmp_path)
    (tidy.ARCHIVE / "a.md").write_text("dup")
    (tidy.DESIGN / "a.md").write_text("dup")
    (tidy.ACTIVE / "HANDOFF.md").write_text("live")
    tidy.main(["--file"])
    assert (tidy.DESIGN / "a.md").exists(), "design/ is not tidy.py's to file"


def test_empty_active_still_audits_memory(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    tidy.MEMORY.mkdir(parents=True)
    (tidy.MEMORY / "MEMORY.md").write_text("- [Gone](gone.md) — dangling\n")
    assert tidy.main([]) == 1
    out = capsys.readouterr().out
    assert "empty — no handoff" in out
    assert "missing file: gone.md" in out, "the audit must not stop at active/"


def test_memory_links_survive_markdown_shapes(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    tidy.MEMORY.mkdir(parents=True)
    (tidy.MEMORY / "real.md").write_text("x")
    (tidy.MEMORY / "MEMORY.md").write_text("- [Real (v2)](./real.md#hook) — parens and prefix\n")
    (tidy.ACTIVE / "HANDOFF.md").write_text("live")
    tidy.main([])
    out = capsys.readouterr().out
    assert "missing file" not in out, "normalised link shapes must resolve"


def test_budget_report_reflects_post_move_state(tmp_path, capsys):
    tidy = load_tidy(tmp_path)
    for i in range(7):
        (tidy.ARCHIVE / f"d{i}.md").write_text(f"dup {i}")
        (tidy.ACTIVE / f"d{i}.md").write_text(f"dup {i}")
    (tidy.ACTIVE / "HANDOFF.md").write_text("live")
    tidy.main(["--file"])
    out = capsys.readouterr().out
    assert "past the one-sitting budget" not in out, (
        "the budget must describe active/ as the moves left it"
    )
