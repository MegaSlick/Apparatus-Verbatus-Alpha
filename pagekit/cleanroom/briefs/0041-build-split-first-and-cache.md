# Brief 0041: split-first defaults and the stage cache (spec 0008)

- Role: build side.
- Issued by the host session at 2026-10-04T17:38:57Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  80d71ffb4f044a5f69318d857f0386635256573cf07553f1034376d2652b06db
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Eighth follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules. Do this after briefs 0039 and 0040 are finished and reported. First merge work/pagekit-prepare.

Build spec 0008 (pagekit/cleanroom/specs/0008-split-first-defaults-and-stage-cache.md), read in full with specs 0002, 0004, 0005 and 0007 for context. The lead has decided that pagekit's default focuses on preserving the original and splitting left and right: the page-box and content-box crops are off by default and turned on by `--crop page` or `--crop content` or an override; with them off, a page is its whole side of the cut, levelled, on a canvas holding the whole levelled side. Add the stage cache: for each source and page, full-resolution lossless images and thumbnails of each step, keyed by source sha256 and the step's inputs hash, reused when unchanged, deletable without loss, never used to make an output page, never inside the source folder, linked from the review sheet. Restate preservation of the original as a test (bytes and modification time unchanged).

Note that this changes the default output: the defaults pin of spec 0007 will need its pinned data regenerated for the new default, and a second pin with `--crop content` must still equal the pages before this change. Say so in the commit. For every behaviour in the spec's test list, add a failing synthetic test first, then the code. Run the pagekit suite under the check script's settings and check-static. Report the head and what each test proved.
