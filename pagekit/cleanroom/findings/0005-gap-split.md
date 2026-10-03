# Finding: splitting a spread when there is no visible fold

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Some spreads show no fold line: the scan is flat, the gutter is pale, or the line is hidden by writing. The split must then come from the layout: the widest empty channel between the two pages' writing. One facing page may be nearly blank, which leaves only a few specks on that side, and the writing of one page may run right up to the middle.

## Pagekit's observed behaviour

Partly covered by the check, not by detection. Pagekit's gutter test searches for the lowest-ink run of columns near the middle and compares a declared split with it, so a split that lands inside writing is flagged. It does not propose a split, and a page with no blank gutter can mislead it. Proposing a split is not built yet.

## General technique

Binarise, remove specks and dark border or shadow bands, and reduce the writing to the boxes around its connected components so that gaps between letters do not count. Project those boxes onto the horizontal axis to get runs of columns with content and runs without. Candidate cuts are the gaps between runs; prefer a gap that divides the content into two comparable halves, and among comparably balanced gaps prefer the widest. If no gap gives a reasonable balance, one page is probably empty: cut at the middle of the frame or just outside the single block of writing, on the side with more space. Ignore tiny runs at the outer edges before scoring, since they are usually specks or edge debris. Estimating the skew first and cutting in the deskewed frame lets the cut lean with the page.
Source: T. M. Breuel, "Two geometric algorithms for layout analysis", Document Analysis Systems, 2002; general knowledge

## Settings in general terms

The smallest gap that counts as page separation depends on the normal space between words and between columns, so it scales with the writing size and the resolution. The minimum balance between halves depends on how blank a facing page can be in the series. How much edge debris may be dropped before scoring should be a small share of the total content width. Whether a weak balance falls back to the middle or sends the frame to review should favour review.
