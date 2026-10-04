# Brief 0021: fix the review findings on orientation and the split (follow-up to brief 0017)

- Role: build side.
- Issued by the host session at 2026-10-04T12:53:55Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  70d72ffec61605e6dfecee2a62386e8eff96b851ad4032d5f81659ef3c4e0160
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0017, same worktree, branch and rules (clean-room rule, allowed paths, checks, commit trailers, stop conditions all unchanged). An independent build-side reviewer, who saw no GPL source, found that orientation never gave a confident wrong turn, but the split detector gives confident, unflagged wrong cuts in three cases. For each, add a failing test with that case first, then fix. Report the head and what each test proved.

1. A vertical rule on a single page is cut as a fold, with no flag (split.py around 487-515 and 534-536). Case A: a 1000x1400 portrait page at 300 dpi with a 4 px ruled line at x=500 gives 2 pages, fold, confidence 0.48, no flags, while its own evidence says the proportions suggest 1 page and 15 marks cross the cut on both sides. Case B: a landscape 1800x1300 single page with writing running across the rule gives 2 pages, confidence 0.60, no flags. Fix: writing that runs through the line on both sides means it is a rule, not a fold. When more than a few marks straddle a fold cut with ink on both sides, flag (or answer 1 page with a flag). Treat "line only, proportions say 1 page" as the gap branch already does.
2. The fold-line score counts writing and texture as line (split.py around 131-172). Coverage is the share of top-hat pixels in a column with no comparison to neighbouring columns, so 50% binary noise gives "2 pages, fold, about 0.6" for 7 of 12 seeds, and dense synthetic handwriting already scores 45-59% against a 60% threshold. Fix: score a column's coverage against the median of its neighbours, or use the longest unbroken run, so only a line that stands out counts. Add noise and dense-writing tests.
3. A spread with one blank page and no fold is called one page, with no flag (split.py around 573-620). Case: 2000x1400 at 300 dpi, writing on the left page only, gives 1 page, confidence 0.61, no flags, while the evidence says the proportions suggest 2. Fix: when the proportions say 2 and all the content sits on one side of the middle, flag "possible spread with a blank page". Keep the single page centred on a pale backdrop unflagged.
4. Test gaps, each of these mutations left all 46 tests passing:
   - the up-down vote with only one of its two cues;
   - fold-line against shadow-valley disagreement;
   - the edge-band filter;
   - the split's too-little-ink check after masking;
   - straight-mark masking in the split;
   - fold-line masking;
   - the negative-page erosion test (the dark-backdrop test never reaches it);
   - dark-border trimming in orientation;
   - preferring the widest gap;
   - the proportions confidence factors.
   Add a test for each that would fail under its mutation. Also exercise the overhang flag from the right reach, not only the left.
5. Optional: report confidence near 0.2 or lower, not about 0.5, when orientation is uncertain, and say why in the evidence. Dense pages often get a weak "thin line" flag and sometimes a spurious neighbour-strip flag, which would put many real pages into review. If finding 2's fix removes those, say so; otherwise reduce them where you can do so without hiding a real fold.
