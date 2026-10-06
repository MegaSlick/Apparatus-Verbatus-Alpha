# Brief 0046: flag two pages decided by an empty band alone

- Role: build side.
- Issued by the host session at 2026-10-04T20:24:01Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  bc019117ae2224d36daa78001f8841d80b978d60e78dd2defaa589bdef7a8f57
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0045, same worktree (work/pk-split), same rules. First merge work/pagekit-prepare. The reviewer re-ran its own set on your fixes: both findings fixed, no spread wrong without a flag, every flourish across the gutter flagged, the split faster. One older failure remains: a single landscape sheet (420 x 297 mm) with two text columns and a 30 mm gap between them is cut into two pages at confidence 0.73 with no flag; the same for printed two columns and for a map whose middle is empty, at 150 and 300 dpi. Cause: a content gap alone decides two pages even with no fold, no shadow and the paper running unbroken across the middle.

The host has decided: a two-page answer decided by a content gap alone, with no fold line, no fold shadow or valley, and no break in the paper across the gap (no change in paper tone, no edge or step at the gap), is flagged ("two pages decided from an empty band alone; the paper runs unbroken across it; check whether this is one sheet"), still with the two-page answer and the cut placed as now. Where the paper does break at the gap (a tone step, a faint shadow, an edge), the gap may decide without this flag. Add failing synthetic tests first on sheets you build yourself (a landscape sheet with two handwritten columns, two printed columns, a map with an empty middle, at 150 and 300 dpi, all four turns: two-page answer with this flag), and check that spreads with any real gutter cue are not flagged by it. Report the head, the counts on your 19 kinds of spread (how many correct spreads this rule now sends to review), and what each test proved.
