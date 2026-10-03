# Finding: smoothing the outlines of strokes after thresholding

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

After thresholding, stroke outlines are ragged. The document records that its reference tool applies a fixed set of shape-replacement rules to remove small bumps and fill small notches along edges, for print-like crispness, on by default. Its notes judge this harmful for handwriting: at common scan resolutions the removed bumps and notches are the size of pen-lift serifs, hairline joins and i-dots.

## Pagekit's observed behaviour

Not built yet. Pagekit produces no output image.

## General technique

Edge regularisation by hit-or-miss transforms or small openings and closings is standard binary morphology. For manuscripts the safe choice is not to do it on any reader image, and on diagnostic binary images to keep it weaker than the smallest meaningful mark. If outlines must be smoother, smooth the grey image before thresholding rather than editing the binary shapes afterwards.
Source: J. Serra, Image Analysis and Mathematical Morphology, Academic Press, 1982

## Settings in general terms

Any structuring element must be smaller than the thinnest meaningful stroke, so it depends on pen width and resolution. Whether it runs at all should be decided by recognition results, with the default off for handwriting.
