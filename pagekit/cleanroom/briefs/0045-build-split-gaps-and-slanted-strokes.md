# Brief 0045: split: gaps bridged by a mark; slanted strokes across the fold

- Role: build side.
- Issued by the host session at 2026-10-04T19:18:33Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  10aa6fb7bb3bf97bb73683da64e7246d2f58ded4bc2b01623d4a51effe0c0794
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0042, same worktree (work/pk-split), same rules. First merge work/pagekit-prepare. The reviewer built 27 kinds of frame of its own (216 runs, 150 to 600 dpi, all four turns): no confident wrong cut and no single page silently cut in two, and no page wrong without a flag in the count. But two failures block, both already present before your last change. For each, a failing synthetic test first on spreads you build yourself (do not read or run the reviewer's scripts), then the fix. Report the head, what each test proved and your table.

P1. Two pages silently made one. A spread at 300 dpi with a 28 mm gutter, no fold line and no shadow, where one flourish crosses the gutter: the split answers one page at confidence 0.54 with no flag ("no fold line, no fold shadow and one mass of writing"), though the frame's proportions (about 1.35) suggest two pages; prepare gives no flags. Same at 150 dpi. Cause: a single mark's box bridges the gap. Fix: let a gap count when only a few marks cross it (up to the same limit you use for marks crossing a fold), and whenever the proportions suggest two pages but no cue decides, flag rather than answer one page silently.
P2. A slanted stroke across a fold line is lost without a flag. A straight stroke from 30 mm left to 12 mm right of a thin fold line, sloping 0.3 (also slope 1, and curved flourishes), gives "no ink mark crosses the cut" and no flag; the same stroke level is counted (11.7 mm overhang) and flagged. In a batch of flourish spreads every run was cut 0.2 mm from the gutter with no flag although each stroke goes more than 3 mm past the cut on both sides. Likely cause: the fold-line mask bridges marks only along rows. Fix: find marks crossing the cut from ink connected across the line in two dimensions (for example after a small closing), or count them before the line is masked; flag any stroke that cannot be whole on either page. Tests: slopes 0, 0.3, 1 and curved, both directions, at 150, 300 and 600 dpi.

Also:
- thresholds_split.toml says overlap_mm 5.0 while prepare uses 3.0 and passes it to the split; make one source of truth so they cannot disagree.
- The "narrow columns of items" rule now flags some ordinary pages (a handwritten spread with 60% of its ink judged to be in narrow columns, a handwritten landscape page, a page with a wax seal). Flags are safe, but check whether ordinary running writing is being read as narrow columns, and tighten the rule if it can be done without losing the account-page cases.
- Speed: the split detector went from about 1.0 to 1.4 s on a 7000x5000 spread; keep it under 1.5 s.

Keep both real spreads unchanged. Record new settings as UNMEASURED with the kinds checked.
