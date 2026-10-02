# Index of finding reports

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

- 0001-orientation-quarter-turn.md: a page scanned on its side, a quarter turn from upright.
- 0002-orientation-upside-down.md: a page or a single leaf that is upside down.
- 0003-spread-or-single.md: deciding whether a frame holds one page or a two-page spread.
- 0004-gutter-line.md: splitting a spread at the fold or gutter.
- 0005-gap-split.md: splitting a spread when there is no visible fold.
- 0006-slanted-split.md: a split line that leans because the book was not square.
- 0007-neighbour-offcut.md: a single page with a strip of the neighbouring page in the frame.
- 0008-gutter-marginalia.md: writing that runs into the fold or sits in the gutter margin.
- 0009-deskew.md: a page that is slightly rotated.
- 0010-deskew-distractors.md: ruled lines, page edges and shadow bands that mislead skew estimation.
- 0011-skew-outliers.md: a page whose skew, content size or margins are unlike the rest of the volume.
- 0012-skew-limits.md: skew estimation failing on blank pages, steep skew or mixed angles.
- 0013-polarity.md: light writing on a dark ground, such as a negative microfilm frame.
- 0014-resolution-metadata.md: missing, wrong or unequal resolution metadata.
- 0015-page-box.md: finding the physical edge of the paper inside the frame.
- 0016-content-box.md: choosing the content region, including marginalia, signatures and marks.
- 0017-dark-band-with-light-text.md: a dark band that carries light writing or is part of the page, not a shadow.
- 0018-ruled-lines.md: ruled lines and long straight marks on the page.
- 0019-scanning-targets.md: colour checkers, rulers and call-number slips in the frame.
- 0020-margins-padding.md: the margin added around the content, and what fills it.
- 0021-blank-page.md: a blank or nearly blank page.
- 0022-dewarp.md: a page curved near the binding, so lines bend.
- 0023-uneven-illumination.md: uneven lighting, gutter shadow and gradual staining across the page.
- 0024-paper-colour-cast.md: yellowed or tinted paper that should be balanced to neutral.
- 0025-binarisation.md: black-and-white output of faded, uneven handwriting.
- 0026-stroke-edge-smoothing.md: smoothing the outlines of strokes after thresholding.
- 0027-despeckle.md: removing specks without removing dots, accents and abbreviation marks.
- 0028-picture-zones.md: keeping pictures in tone while thresholding text (mixed output).
- 0029-colour-reduction.md: colour segmentation and posterisation that flatten ink shading.
- 0030-bleed-through.md: show-through and bleed-through from the other side of the leaf.
- 0031-resample-once.md: rotating, cropping and scaling without repeated blur, and the output resolution.
- 0032-faint-ink-tone.md: faint iron-gall ink that needs more contrast in a grey image.
- 0033-manual-overrides.md: keeping a person's correction when earlier steps are re-run.
- 0034-coordinate-provenance.md: mapping coordinates on a prepared image back to the archival scan.
- 0035-measuring-success.md: deciding whether a preparation step actually helps recognition.

## What was left out

Much of the document is a step-by-step account of how the other program's code works, and I left that out because it could not be passed on without carrying their expression. The omitted material covers: the exact order and composition of its filters inside each stage; every numeric constant, window size, weight table, search range and acceptance threshold; the internal layout of its program, its data structures, its project file format and its user-interface controls; the specific recipes it uses for gutter-line enhancement, gap scoring, content-block segmentation and edge trimming, background estimation, picture detection, despeckle anchoring and posterisation; the internals of its curve tracing and surface model for dewarping; and two behaviours the document calls defects in that code. The reports keep only the page situations, the published or textbook methods that address them, and what each setting should depend on. The lead's own suggested values (overlaps, margins, resolution caps, tolerances and test targets) are also left as "to be measured", so that no number reaches the build side from the document.
