# Finding: deciding whether a frame holds one page or a two-page spread

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Before a frame can be split, something must decide whether it holds one page, two facing pages, or one page with a strip of its neighbour. The document records that its reference tool decides this only from the frame's proportions after rotation (wider than tall means two pages), and uses the content only to decide where to cut. Microfilm frames and archive or genealogy-site scans often have odd framing, so proportions alone mislead: a single page with a wide backdrop looks like a spread, and a tightly framed spread of tall pages can look like a single page.

## Pagekit's observed behaviour

Not built yet. Pagekit does not decide how many pages a frame holds. It takes the crops, and optionally a split position, as declared input. With two side-by-side crops it infers the split as midway between their facing edges, and it then checks that split against the gutter.

## General technique

Decide the page count from evidence and record which evidence decided. Three kinds are available: a fold or gutter line near the middle (a thin dark line or a soft dark valley in brightness running roughly top to bottom), a wide gap in the content near the middle, and two separate masses of writing each with its own left and right edges. Physical proportions, using the resolution recorded for each axis, are a prior rather than a verdict. When the cues disagree, or none is strong, the frame goes to review.
Source: general knowledge

## Settings in general terms

The prior from proportions depends on the expected page shape of the series, so it belongs in per-collection configuration. How close to the middle a fold may lie depends on how consistently the frames are centred. The strength a gutter or gap must have before it counts should be measured on spreads and single leaves from the actual collection.
