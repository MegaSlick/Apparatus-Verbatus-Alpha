#!/usr/bin/env python3
"""Report on the state of workbench/. Changes nothing.

The mechanical half of the session-start and session-end procedures. It exists so
that a model running at high context is answering questions rather than composing
shell — and so that the answers are measured rather than remembered.

    python3 .githooks/tidy.py

**It reads and reports; it never moves, writes or deletes.** It once carried a
`--file` mode that moved provably duplicated notes into `scratch/`, and that mode
had no caller: `session-start` runs the report, and `session-end` runs the report
and then makes the archive move itself, deliberately, because only the session that
did the work can say which notes are finished. A mover nothing invoked was a
mutation path on the drawer holding the live handoff, kept alive by its own
docstring. Filing stays a judgement a session makes with its hands.

Exit status is 0 when nothing wants attention, 1 when something does, and 2 when
the check itself broke. **Exit 1 is the ordinary, informative case** — it means the
report has something in it, not that the tool failed. Only 2 is a failure, and a
caller must treat it as one and never as a pass — GOVERNANCE.md 10.
"""

import argparse
import hashlib
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKBENCH = REPO / "workbench"
ACTIVE = WORKBENCH / "active"
STANDING = WORKBENCH / "standing"
DESIGN = WORKBENCH / "design"
ARCHIVE = WORKBENCH / "archive"
SCRATCH = WORKBENCH / "scratch"
RAW = WORKBENCH / "raw"
AUTOCLAVE = WORKBENCH / "autoclave"
QUARANTINE = WORKBENCH / "quarantine"

# A session never deletes from quarantine/; it stages material there and Tyrel
# deletes it. This is how long something may sit before the session says so
# unprompted — his instruction, 2026-08-02, when the drawer was created.
QUARANTINE_RIPE_DAYS = 7

# The one ledger in standing/ whose absence is itself a finding: CLAUDE.md records a
# dated suspension there and reads it back until it is resolved.
SUSPENSIONS = "SUSPENSIONS.md"

# active/ is meant to be readable in one sitting. Past this, something finished
# and nobody filed it. The number is a smell test, not a rule.
ACTIVE_FILE_BUDGET = 6
ACTIVE_BYTE_BUDGET = 120_000

# raw/ is measured but never budgeted like active/. A transcript is not a note
# and nobody reads one in a sitting; the mark below only asks whether closed
# runs have been archived, which is a session-end job.
RAW_BYTE_BUDGET = 2_000_000

# A note nobody has touched in this long is probably describing finished work.
# It is reported, never moved: only the session that did the work can say.
STALE_DAYS = 3

# Never reported as a duplicate, however byte-identical it looks.
#
# session-end copies the outgoing handoff into archive/ and then overwrites it. A
# session interrupted between those two steps — or one whose new handoff happens to
# match the old — leaves active/HANDOFF.md identical to the archived copy. That is
# the procedure working, not a note somebody forgot to file, and reporting the live
# handoff as redundant invites exactly the one deletion that cannot be recovered.
#
# NEXT_SESSION_BRIEF.md is archived by the same step, in the same order, and one
# sentence of the session-end skill covers both: "Copy the outgoing
# workbench/active/HANDOFF.md and NEXT_SESSION_BRIEF.md there before overwriting
# either." Only the handoff was listed here, so an interrupted close left the live
# brief reported as already archived — the file the next session opens with, offered
# for deletion by the report that is supposed to protect it.
NEVER_FILED = {"HANDOFF.md", "NEXT_SESSION_BRIEF.md"}

# Claude Code derives this directory name from the repository's absolute path,
# with both separators and underscores flattened to hyphens. Deriving it rather
# than hardcoding it means a second clone, a worktree or another machine reports
# on its own memory instead of complaining that Tyrel's is missing.
MEMORY = (
    Path.home() / ".claude" / "projects" / str(REPO).replace("/", "-").replace("_", "-") / "memory"
)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def archived_digests():
    """Every file under archive/, indexed by content.

    Indexed by hash rather than by name so a note that was archived under a
    different filename still counts as already filed.
    """
    seen = {}
    if not ARCHIVE.is_dir():
        return seen
    for path in sorted(ARCHIVE.rglob("*")):
        if path.is_file():
            seen.setdefault(digest(path), path)
    return seen


def active_files():
    if not ACTIVE.is_dir():
        return []
    return sorted(p for p in ACTIVE.rglob("*") if p.is_file())


def rel(path):
    return path.relative_to(REPO)


def rel_or_abs(path):
    """`rel`, but safe for paths that may sit outside the repository."""
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def check_memory():
    """Does every line of the memory index point at a file that exists, and does
    every file have a line?

    Cheap, and it catches the failure that matters: an index entry whose file was
    deleted still costs context every session and points at nothing.

    Returns (dangling, unlisted, note). `note` is set when the check could not run
    at all — a missing memory directory is *not applicable*, not a finding, because
    a clone that has never held a session legitimately has none.
    """
    index = MEMORY / "MEMORY.md"
    if not index.is_file():
        return [], [], f"no memory index at {index} — not checked"
    linked = set()
    for line in index.read_text().splitlines():
        # Every link on the line, not just the first. The index is one memory
        # per line by convention, but a line that mentions a second memory —
        # "see also [other](other.md)" — used to have that second target read
        # as unlinked, so tidy reported a live memory as an orphan.
        pos = 0
        while True:
            start = line.find("](", pos)
            if start == -1:
                break
            end = line.find(")", start + 2)
            if end == -1:
                break  # an unclosed link is malformed, not a dangling target
            pos = end + 1
            raw = line[start + 2 : end].strip()
            # Normalise the shapes markdown allows: <angle-bracketed> targets,
            # ./ prefixes, and #anchors all point at the same file.
            #
            # Angle brackets are how markdown spells a target containing a
            # space, so the target ends at the closing '>' and not at the
            # first space — splitting on whitespace first turned
            # `<spaced name.md>` into `spaced`, which reported a live memory
            # as dangling and its file as unlisted, both wrongly.
            if raw.startswith("<"):
                close = raw.find(">")
                target = raw[1:close] if close != -1 else raw[1:]
            else:
                # Outside angle brackets a space begins the optional title.
                target = raw.split()[0] if raw else ""
            target = target.removeprefix("./").split("#")[0]
            if target and not target.startswith(("http:", "https:")):
                linked.add(target)
    # Compare on paths relative to the memory directory, so a link into a
    # subdirectory is matched rather than reported dangling on its bare name.
    present = {str(p.relative_to(MEMORY)) for p in MEMORY.rglob("*.md")} - {"MEMORY.md"}
    # Dangling is resolved against the filesystem, not against `present`: an
    # index entry pointing at a non-.md file is unusual but not missing, and
    # reporting it as missing is a false alarm. `unlisted` stays .md-scoped.
    dangling = sorted(name for name in linked if not (MEMORY / name).exists())
    unlisted = sorted(present - linked)
    return dangling, unlisted, None


def main(argv=None):
    # No options. The parser stays so that --help works and an unrecognised
    # argument is refused rather than silently ignored — a caller passing the
    # retired --file must be told it is gone, not quietly given a report it
    # believes performed a move.
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    if not WORKBENCH.is_dir():
        print(f"workbench/ not found at {WORKBENCH}", file=sys.stderr)
        return 2

    archived = archived_digests()
    files = active_files()
    now = time.time()

    duplicates = []
    stale = []
    live = []
    for path in files:
        match = archived.get(digest(path))
        if match is not None and path.name not in NEVER_FILED:
            duplicates.append((path, match))
        elif (now - path.stat().st_mtime) > STALE_DAYS * 86400:
            stale.append(path)
        else:
            live.append(path)

    total_bytes = sum(p.stat().st_size for p in files)
    wants_attention = False

    # A missing active/ is not the same as an empty one, and neither is "clean":
    # both skills say an absent handoff is a fact worth reporting out loud. But
    # neither ends the audit — the design, scratch and memory checks below run
    # regardless, because a fresh clone is exactly where memory drift matters most.
    if not ACTIVE.is_dir():
        wants_attention = True
        print(f"active/   MISSING at {rel_or_abs(ACTIVE)} — no handoff to read.")
    elif not files:
        wants_attention = True
        print("active/   empty — no handoff. Say so rather than guessing.")
    else:
        print(f"active/   {len(files)} files, {total_bytes // 1024} KB")
        for path in live:
            print(f"  in play   {rel(path)}")

    if duplicates:
        wants_attention = True
        print(f"\nalready archived, byte-identical — {len(duplicates)}:")
        for path, match in duplicates:
            print(f"  {rel(path)}  ==  {rel(match)}")
        print("  -> archived already. Delete from active/ only if you know the work closed.")

    if stale:
        wants_attention = True
        print(f"\nuntouched for over {STALE_DAYS} days — {len(stale)}:")
        for path in stale:
            age = int((now - path.stat().st_mtime) // 86400)
            print(f"  {rel(path)}  ({age}d)")
        print("  -> not moved. Only a session that knows the work can file these.")
        print("     Ages are mtime, which a copy or a fresh clone resets. Treat as a")
        print("     prompt to look, not as a measurement.")

    if len(files) > ACTIVE_FILE_BUDGET or total_bytes > ACTIVE_BYTE_BUDGET:
        wants_attention = True
        print(
            f"\nactive/ is past the one-sitting budget "
            f"({len(files)}/{ACTIVE_FILE_BUDGET} files, "
            f"{total_bytes // 1024}/{ACTIVE_BYTE_BUDGET // 1024} KB)."
        )

    # standing/ holds the ledgers that outlive sessions — suspensions, alpha
    # shortcuts, the adopted plan. Never filed, never aged, never counted against
    # active/'s budget: a drawer for what must persist is not a drawer over budget.
    #
    # Its absence is reported as loudly as a missing active/, and for a harder
    # reason. CLAUDE.md carries a suspension in workbench/standing/SUSPENSIONS.md and
    # has it read back at every open and close until it is resolved; a hook or a check
    # switched off temporarily is exactly what that file is for. A drawer that is not
    # there prints nothing, which reads identically to a drawer with nothing in it —
    # so a safety measure would go quietly on being off, which is the failure this
    # project exists to notice. Unknown is not zero: only a ledger somebody wrote can
    # say that nothing is suspended.
    if not STANDING.is_dir():
        wants_attention = True
        print(f"\nstanding/ MISSING at {rel_or_abs(STANDING)} — {SUSPENSIONS} cannot be read.")
        print("  -> unknown is not 'none in force'. Only a written ledger says that.")
    else:
        standing = sorted(p for p in STANDING.rglob("*") if p.is_file())
        if standing:
            print(f"\nstanding/ {len(standing)} ledgers — read at open and close, never filed:")
            for path in standing:
                print(f"  {rel_or_abs(path)}")
        # Read it, not stat it: is_file() is true for a ledger this process cannot
        # open, and a report that exits clean over an unreadable safety ledger has
        # measured nothing (GOVERNANCE 10). The two failures get different words
        # because their fixes differ.
        try:
            (STANDING / SUSPENSIONS).read_text(encoding="utf-8")
        except FileNotFoundError:
            wants_attention = True
            print(f"\nstanding/ has no {SUSPENSIONS} — the dated suspensions cannot be read back.")
            print("  -> unknown is not 'none in force'. Only a written ledger says that.")
        except OSError as error:
            wants_attention = True
            print(f"\nstanding/ {SUSPENSIONS} exists but cannot be read: {error}")
            print("  -> unknown is not 'none in force'. Only a readable ledger says that.")

    # design/ is never aged, never filed, never counted against the budget. A
    # proposal waiting for the stage it concerns is not stale, however long it
    # waits, and staleness is the only signal this tool could offer it.
    design = sorted(p for p in DESIGN.rglob("*.md")) if DESIGN.is_dir() else []
    if design:
        print(f"\ndesign/   {len(design)} waiting — re-read each before the stage it concerns:")
        for path in design:
            print(f"  {rel(path)}")

    scratch_count = len([p for p in SCRATCH.rglob("*") if p.is_file()]) if SCRATCH.is_dir() else 0
    if scratch_count:
        print(f"\nscratch/  {scratch_count} files — disposable, delete whenever.")

    # quarantine/ is the staging drawer for material a session believes is dead.
    # Nothing is deleted here by a session: it is moved in, and Tyrel deletes it.
    # The report exists because a staging drawer nobody empties is just a slower
    # kind of clutter — after a week, the session says so rather than waiting to
    # be asked. Ages are mtime, which a move resets, so the clock starts when the
    # material was quarantined rather than when it was written. That is the age
    # this drawer is actually about.
    quarantine = [p for p in QUARANTINE.rglob("*") if p.is_file()] if QUARANTINE.is_dir() else []
    if quarantine:
        q_bytes = sum(p.stat().st_size for p in quarantine)
        print(
            f"\nquarantine/ {len(quarantine)} files, {q_bytes // 1024} KB — staged for HIS deletion."
        )
        cutoff = time.time() - QUARANTINE_RIPE_DAYS * 86400
        top = sorted(
            {p.relative_to(QUARANTINE).parts[0] for p in quarantine if p.stat().st_mtime < cutoff}
        )
        if top:
            wants_attention = True
            print(
                f"  -> {len(top)} item(s) have sat over {QUARANTINE_RIPE_DAYS} days. "
                f"Tell him; he decides whether they go:"
            )
            for name in top:
                print(f"     {name}")

    # raw/ holds verbatim engine output: Codex transcripts, reviewer logs, the
    # things a finding must be able to point at. It is evidence, so unlike
    # scratch/ nothing here is disposable on sight — and unlike active/ it is
    # not held to the one-sitting budget, because a 200 KB transcript is not
    # something anyone reads in a sitting. Filing raw logs in active/ is what
    # pushed that drawer over budget in the first place.
    raw_files = [p for p in RAW.rglob("*") if p.is_file()] if RAW.is_dir() else []
    if raw_files:
        raw_bytes = sum(p.stat().st_size for p in raw_files)
        print(
            f"\nraw/      {len(raw_files)} engine logs, {raw_bytes // 1024} KB — evidence, not notes."
        )
        if raw_bytes > RAW_BYTE_BUDGET:
            wants_attention = True
            print(
                f"  -> past {RAW_BYTE_BUDGET // 1024} KB. Archive the runs whose work has "
                f"closed; keep the ones a live finding still cites."
            )

    # autoclave/ is the one drawer nothing here creates on a clock and nothing
    # empties: `operations/autoclave/autoclave.sh` writes one directory per dispatched
    # chamber, holding the brief, the report and the collected bundle, and
    # `autoclave.sh rm` keeps it on purpose — the bundle is the only surviving evidence
    # that a dispatch happened. So it is counted and named here and judged no further:
    # like raw/ it is archived with the work that cites it rather than on a clock of
    # its own, and it is held to no budget. Reported without demanding attention,
    # because a drawer whose whole point is that it survives is not a finding every
    # session. Before this it was the only drawer a session could not see at all.
    chambers = (
        sorted(p.name for p in AUTOCLAVE.iterdir() if p.is_dir()) if AUTOCLAVE.is_dir() else []
    )
    if chambers:
        chamber_files = [p for p in AUTOCLAVE.rglob("*") if p.is_file()]
        chamber_bytes = sum(p.stat().st_size for p in chamber_files)
        print(
            f"\nautoclave/ {len(chambers)} chamber drawers, {chamber_bytes // 1024} KB — "
            f"dispatch evidence, kept when the chamber is destroyed:"
        )
        for name in chambers:
            print(f"  {name}")

    dangling, unlisted, note = check_memory()
    if note:
        print(f"\nproject memory: {note}")
    elif dangling or unlisted:
        wants_attention = True
        print("\nproject memory:")
        for name in dangling:
            print(f"  index points at a missing file: {name}")
        for name in unlisted:
            print(f"  file is in no index line: {name}")

    if not wants_attention:
        print("\nnothing wants attention.")
    return 1 if wants_attention else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a broken check must never read as a pass
        print(f"tidy.py failed: {exc}", file=sys.stderr)
        sys.exit(2)
