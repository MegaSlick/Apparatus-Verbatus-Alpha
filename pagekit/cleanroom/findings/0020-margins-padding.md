# Finding: the margin added around the content, and what fills it

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

After the content box is found, the output crop adds some margin so writing is not flush with the edge. The margin may run past the paper edge, which leaves area with no paper to fill. The document records that its reference tool sets margins in millimetres, can split the gap between page box and content box proportionally, fills margins with either white or the measured paper colour, and by default pads every page of a volume to the same size with an alignment rule. The lead's document judges same-size padding useless for recognition (it adds dead pixels) and asks for it to be optional, with the margin clamped to the page box plus a small overshoot and filled with the paper colour.

## Pagekit's observed behaviour

Not built yet. Pagekit never proposes or changes a crop; it only checks declared crops.

## General technique

Pad the content box by a fixed physical margin, clamp the result to the page box plus a small allowance, and fill any area outside the paper with an estimated paper colour, so the reader sees a natural sheet rather than a hard white or black frame. Estimate the paper colour from the light class of the page: take the pixels above a global threshold, and use a robust central value of those pixels, such as the median or the mode, so ink and stains do not pull it. Measure margins in physical units through the original resolution, so they hold under rotation. Make cross-page size matching an option for print-style output only.
Source: general knowledge

## Settings in general terms

The margin depends on how much context the downstream readers benefit from and on the size of the writing, and should be tuned by recognition results. The overshoot allowed past the paper edge depends on how accurate the page box is. The paper colour estimate depends on a reliable split between ink and paper, which itself depends on the page's contrast.
