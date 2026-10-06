# Brief 0042: harden the split; last account-page case

- Role: build side.
- Issued by the host session at 2026-10-04T17:39:30Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  ebe09a921dcddc97dc0dc3935e814fea91c60f2ba7bcb60b13b13bb34889d7da
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0038, same worktree (work/pk-split), same rules. First merge work/pagekit-prepare. The reviewer's last re-test of cb623d0 found no regression. The lead has now set the priority: pagekit's default is to preserve the original and split left and right properly; cropping is off by default (spec 0008, being built by another agent). So the split is now the most important detector. Do these, each with failing synthetic tests first on pages you build yourself (do not run the reviewer's generators).

S1. Harden the split on a wide range of spreads you build: tight bindings where the gutter curls into a deep shadow; a gutter shadow on one side only; unequal page widths; the spread off-centre in the frame; a spread rotated a few degrees in the frame; one page blank or nearly blank; two pages of different paper tone; writing, a table or a signature that runs across the gutter; a slip or insert lying across the gutter; a page-edge stack and a dark board on either side; microfilm-style frames with a dark border; low contrast between gutter and paper; faint ink; dense cursive; 150 to 600 dpi; all four orientations after the quarter turn. For each kind, the answer must be the right count and the cut within a small tolerance of the drawn gutter line, or a flag; never a confident wrong cut and never one page silently where there are two. Report a table: kind, runs, right, flagged, wrong without flag, and the cut error.
S2. Where a cut cannot avoid writing (writing across the gutter), make sure the overlap and the overhang flag of spec 0002 and 0003 keep every stroke whole on at least one page, and the flag says so; test it.
S3. The last orientation case: a handwritten account page whose words take about a fifth of the width beside six columns of handwritten figures is turned three quarters with confidence 0.65 and no flag, because handwritten figures join into marks of mixed size, so only 14 of 84 tiles are seen as one-size, below your one-third level, and the figure columns then decide that lines run down. Fix: also test for one-size marks per column of the page, not only per tile, or flag when most of the page's ink lies in narrow vertical runs of marks; test with your own handwritten account pages where figures join.

Keep both real spreads right with no flag. Record every new setting as UNMEASURED with the page kinds checked. Report the head, what each test proved and your tables.
