# Finding: uneven lighting, gutter shadow and gradual staining across the page

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Paper brightness varies across the page: darker toward the gutter, uneven camera lighting, foxing gradients and broad water stains. Faint ink in a dark area may be darker than bare paper in a bright area, so any single threshold or contrast setting fails somewhere. The document records that its reference tool divides the image by a smooth estimated background surface, which flattens the paper to white while ink keeps its relative darkness, and that the correction acts on brightness only. It also records failure cases: a smooth surface cannot follow sharp transitions such as a fold crease or the edge of a water stain, leaving halos; very faint entries can be absorbed into the background estimate; and a page with no usable paper samples yields an empty surface that turns everything white.

## Pagekit's observed behaviour

Not built yet. Pagekit uses one global threshold; its limits note that uneven lighting can move writing to the paper side or stains to the ink side.

## General technique

Flat-field correction by background estimation. Estimate the paper background without the ink, then divide (or subtract) to flatten it. Background estimation methods: fit a low-order two-dimensional polynomial surface by least squares to pixels judged to be paper, rejecting dark outliers iteratively; or use a large median filter or a large morphological closing, which removes strokes and keeps the slowly varying paper. Work on a reduced version for speed and evaluate the surface at full size. Guard the edge cases: too few paper samples means no correction and a flag, not a white page; sharp stain edges call for a local estimator (median or closing) rather than a global smooth surface.
Source: B. Gatos, I. Pratikakis, S. J. Perantonis, "Adaptive degraded document image binarization", Pattern Recognition, 2006; S. Lu, B. Su, C. L. Tan, "Document image binarization using background estimation and stroke edges", International Journal on Document Analysis and Recognition, 2010

## Settings in general terms

The smoothness of the background (polynomial degree, or filter size) must vary more slowly than letters but fast enough to follow the lighting, so the filter size depends on stroke width and letter height, and the polynomial degree on how complex the lighting is across the page. The rejection margin for ink depends on the contrast of the faintest ink to keep. The minimum share of paper samples before a correction is trusted should be measured on heavily written and heavily stained pages.
