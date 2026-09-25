#!/usr/bin/env python3
"""Report on the state of workbench/ for session start and end. Changes nothing:
only the session that did the work can say which notes are finished.

Exit 0: nothing wants attention; 1: the report has something in it (the ordinary
case); 2: the check itself broke, which a caller must never read as a pass.
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
QUARANTINE = WORKBENCH / "quarantine"

# A session stages material in quarantine/ and only the project lead deletes it.
QUARANTINE_RIPE_DAYS = 7

# Absent is itself a finding: a suspended safety check is recorded and read back here.
SUSPENSIONS = "SUSPENSIONS.md"

# A smell test for "readable in one sitting"; set so the drawer is not over budget
# every session, which would report nothing.
ACTIVE_FILE_BUDGET = 16
ACTIVE_BYTE_BUDGET = 400_000

RAW_BYTE_BUDGET = 2_000_000
STALE_DAYS = 3

# session-end copies these into archive/ before overwriting them, so an interrupted
# close leaves live copies byte-identical to the archive. Never offer them for deletion.
NEVER_FILED = {"HANDOFF.md", "NEXT_SESSION_BRIEF.md"}

# Claude Code's naming: the absolute path with separators and underscores as hyphens.
MEMORY = (
    Path.home() / ".claude" / "projects" / str(REPO).replace("/", "-").replace("_", "-") / "memory"
)

# Notes are small; filed evidence can run to gigabytes and is never worth hashing.
ARCHIVE_DIGEST_MAX_BYTES = 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def archived_digests():
    """archive/ indexed by content, so a note filed under another name still counts."""
    seen = {}
    for path in _files_under(ARCHIVE):
        if path.stat().st_size <= ARCHIVE_DIGEST_MAX_BYTES:
            seen.setdefault(digest(path), path)
    return seen


def _files_under(directory):
    return sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []


def active_files():
    return _files_under(ACTIVE)


def rel(path):
    return path.relative_to(REPO)


def rel_or_abs(path):
    """`rel`, but safe for paths that may sit outside the repository."""
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def _link_target(raw):
    # Markdown spells a target containing a space in <angle brackets>; otherwise a
    # space begins the optional title.
    if raw.startswith("<"):
        close = raw.find(">")
        target = raw[1:close] if close != -1 else raw[1:]
    else:
        target = raw.split()[0] if raw else ""
    return target.removeprefix("./").split("#")[0]


def _local_link_targets(line):
    pos = 0
    while (start := line.find("](", pos)) != -1:
        end = line.find(")", start + 2)
        if end == -1:
            return  # an unclosed link is malformed, not a dangling target
        pos = end + 1
        target = _link_target(line[start + 2 : end].strip())
        if target and not target.startswith(("http:", "https:")):
            yield target


def check_memory():
    """(dangling index links, memory files in no index line, note if not checked).

    A clone that never held a session has no memory index: not applicable, not a finding.
    """
    index = MEMORY / "MEMORY.md"
    if not index.is_file():
        return [], [], f"no memory index at {index} — not checked"
    linked = {t for line in index.read_text().splitlines() for t in _local_link_targets(line)}
    present = {str(p.relative_to(MEMORY)) for p in MEMORY.rglob("*.md")} - {"MEMORY.md"}
    # Dangling is checked on disk: a link to a non-.md file is unusual, not missing.
    dangling = sorted(name for name in linked if not (MEMORY / name).exists())
    unlisted = sorted(present - linked)
    return dangling, unlisted, None


def main(argv=None):
    # No options: the parser refuses any argument, so the retired --file never
    # reads as a move that happened.
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

    # Neither a missing nor an empty active/ ends the audit: a fresh clone is where
    # memory drift matters most.
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

    # standing/ ledgers outlive sessions: never filed, aged or budgeted. A missing
    # drawer would print nothing, reading like no suspension in force; unknown is not zero.
    if not STANDING.is_dir():
        wants_attention = True
        print(f"\nstanding/ MISSING at {rel_or_abs(STANDING)} — {SUSPENSIONS} cannot be read.")
        print("  -> unknown is not 'none in force'. Only a written ledger says that.")
    else:
        standing = _files_under(STANDING)
        if standing:
            print(f"\nstanding/ {len(standing)} ledgers — read at open and close, never filed:")
            for path in standing:
                print(f"  {rel_or_abs(path)}")
        # Read, not stat: is_file() is true for a ledger this process cannot open.
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

    # A proposal waiting for its stage is never stale, however long it waits.
    design = sorted(p for p in DESIGN.rglob("*.md")) if DESIGN.is_dir() else []
    if design:
        print(f"\ndesign/   {len(design)} waiting — re-read each before the stage it concerns:")
        for path in design:
            print(f"  {rel(path)}")

    scratch_count = len(_files_under(SCRATCH))
    if scratch_count:
        print(f"\nscratch/  {scratch_count} files — disposable, delete whenever.")

    # Ages are mtime, which a move resets: the clock starts at quarantining, as intended.
    quarantine = _files_under(QUARANTINE)
    if quarantine:
        q_bytes = sum(p.stat().st_size for p in quarantine)
        print(
            f"\nquarantine/ {len(quarantine)} files, {q_bytes // 1024} KB — staged for the "
            "project lead's deletion."
        )
        cutoff = time.time() - QUARANTINE_RIPE_DAYS * 86400
        top = sorted(
            {p.relative_to(QUARANTINE).parts[0] for p in quarantine if p.stat().st_mtime < cutoff}
        )
        if top:
            wants_attention = True
            print(
                f"  -> {len(top)} item(s) have sat over {QUARANTINE_RIPE_DAYS} days. "
                f"Tell the project lead; they decide whether they go:"
            )
            for name in top:
                print(f"     {name}")

    # raw/ is evidence findings cite: never disposable, and not read in a sitting.
    raw_files = _files_under(RAW)
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
