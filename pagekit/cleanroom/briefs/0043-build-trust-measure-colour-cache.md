# Brief 0043: tag trust re-runs, measure with cropping off, colour on coloured paper, cache care

- Role: build side.
- Issued by the host session at 2026-10-04T18:31:13Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  293f80e0e46254a9b26d591e25194b062467cd75f94ece4d701af1896b6accc1
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Ninth follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules. First merge work/pagekit-prepare. An independent build-side reviewer checked ad4a27a: all 80 tag cases match the upright page pixel for pixel, the default canvas keeps every pixel of the side, `--crop content` is byte-identical to before, the cache never changes an output, and the source folder is untouched. Two things block. For each, a failing synthetic test first, then the fix. Report the head and what each test proved.

C1. Changing tag trust on a TIFF re-runs nothing (prepare.py around 588-589): the step inputs include the tag only when the chain applies it, and for a TIFF Pillow applies it, so a trust change leaves every inputs hash the same and old answers are reused on a different grid. Case: an upright page stored as TIFF with a wrong tag 3; run 1 trusted detects two turns and the output is upright; run 2 with tag_trust false reuses the two turns and the output is upside down with no flag (a PNG re-detects correctly). The cache's `opened` key (cache.py around 72) has the same fault. Fix: put the tag actually applied, whoever applied it, and which grid the chain starts from (whether Pillow's turn was undone) into every step's inputs and into the opened cache key.
C2. `measure` marks every page of a default run as wrong without a flag (measure.py around 257-275): with cropping off, the content box is a stand-in for the whole side at confidence 1.0, and measure scores it against the gold box (a 135 mm error). Skip the content and page boxes when they were not applied, with a note saying so, as volume.py already does.

Should fix:
- Colour on coloured paper: the check measures distance from neutral grey, so a pale blue wash over 30% of cream paper (chroma 35 against a threshold of 50) and blue ruling 0.15 to 0.2 mm wide on cream paper are lost when grey is chosen. Measure each pixel's colour difference from the page's own paper colour (estimated from the most common paper chroma, for example in a perceptual colour space or as a chroma vector difference), keep the noise estimate from that difference, and test washes and fine ruling on white, cream and yellow paper; keep faint brown ink on yellow paper made grey as now, and keep the README's list of limits true.
- The review sheet's correction command drops --tone-view, so a corrected page's tone view goes stale and drops out of the manifest: repeat it.
- README: say clearly that for a TIFF the point maps lead to the grid as the library opens the file's bytes (turned upright), not the stored pixels; fix the contradiction around lines 199 and 202 and the stale line about tags being ignored around 551. Update spec-facing wording in the README only; do not edit specs.
- Cache: name the default cache folder after the output folder (not shared by output folders with the same parent); remove entries for sources no longer in the batch; detect a corrupted cache file (by its recorded hash) and rewrite it; refuse --cache inside the output folder; print the cache's size at the end of each run, and warn plainly before a run when the free disk space is less than an estimate of what the cache and outputs will need. Do not change whether full-resolution entries are written by default; the lead is deciding that.
- A version guard for the tag detection: if a future Pillow stops deleting the tag after applying it, detection must still give one application (the header-size fallback); test it by simulating both behaviours.
- Tests that catch these mutations, which survived: dropping or scaling the MAD term; opening side 1 instead of 2; page box not intersected; the opened key ignoring the tag; volume measuring with cropping off; the cache fill colour; a hand-set page box not turning cropping on; padding not repeated in the correction command; stale cache files kept.

Run the pagekit suite under the check script's settings and check-static.
