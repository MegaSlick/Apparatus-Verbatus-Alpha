# Brief 0038: figure tiles out of the up-down vote

- Role: build side.
- Issued by the host session at 2026-10-04T17:14:18Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  6ed5bf8bad155d6d373bcf6f333f22fd63dda1eab5c28467d41b916115379aea
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Follow-up to brief 0036, same worktree (work/pk-split), same rules. First merge work/pagekit-prepare. The reviewer re-ran everything on 2952d93: no regression anywhere, the figure sweep went from 10 wrong to 0, turned blocks are right at 25 to 35 percent and flagged above, every band stroke is now flagged, and both real spreads stay right. One page that the previous head flagged is now wrong without a flag: a handwritten account page at 150 dpi, handwritten words across half the width beside six columns of handwritten figures. 61 of the 103 tiles holding writing are left out as one-size tiles, the remaining 42 decide lines across with no dissent, and then the up-down vote gives a half turn at confidence 0.50 with no flag, because the figure tiles are left out of the line-direction vote but still vote in the up-down strips, and the figures carry their own up-down lean.

Fix, with a failing synthetic test first on pages you build yourself (handwritten account pages with several figure columns, words across a quarter to a half of the width, 150 and 300 dpi, all four turns; right or flagged): leave the one-size marks or tiles out of the up-down strip votes too, and flag the decision when one-size tiles are a large share of the tiles holding writing (choose the level from your own pages, around a third, and record it as UNMEASURED). Keep everything else as it is. Do not run the reviewer's generator. Report the head, what the test proved and your counts.
