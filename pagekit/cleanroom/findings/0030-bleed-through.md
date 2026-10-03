# Finding: show-through and bleed-through from the other side of the leaf

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Iron-gall ink bleeds through thin paper, and writing on the verso shows through. The reversed writing is often as dark as faint writing on the recto, so a threshold promotes it to ink and a reader may try to read it. The lead's document asks not to threshold it away, and to offer only an optional attenuation, softly pulling pixels toward the paper level after background subtraction, as a separate experimental variant.

## Pagekit's observed behaviour

Not built yet. Pagekit's limits note that show-through can move to the ink side of its global threshold, which can cause false discarded-ink flags if it lies outside a crop.

## General technique

Show-through reduction. The simplest approach is soft attenuation after flat-field correction: pixels that are only slightly darker than paper are pulled toward the paper level, with a smooth transition so faint real ink is weakened rather than erased. Stronger approaches register the recto and verso images, mirror the verso, and use it to identify and suppress the reversed writing, or separate the two layers by blind source separation. Keep this as a separate variant whose value is measured by recognition results.
Source: G. Sharma, "Show-through cancellation in scans of duplex printed documents", IEEE Transactions on Image Processing, 2001; A. Tonazzini, L. Bedini, E. Salerno, "Independent component analysis for document restoration", International Journal on Document Analysis and Recognition, 2004

## Settings in general terms

The attenuation range depends on the contrast between show-through and the faintest recto ink, measured on the collection. Registration of recto and verso depends on how consistently the leaves were captured. The variant should be offered, not applied by default.
