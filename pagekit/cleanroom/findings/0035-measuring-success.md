# Finding: deciding whether a preparation step actually helps recognition

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Each preparation step is a guess about what helps the readers. The lead's document proposes a gold set of frames covering two-page spreads, single leaves, rotated frames, heavy bleed-through, faded ink, tight gutters with marginalia and scans with colour targets, with targets for split accuracy (no ink component lost from either page), deskew error, orientation (right or flagged), content box (no ink outside the crop against a hand-drawn mask), and above all recognition error per image variant against the raw scan. It notes that the input requirements of the reader models are not verified and should be found by experiment.

## Pagekit's observed behaviour

Pagekit's thresholds are uncalibrated: each is marked as not yet measured, and its report says so. Its tests pin behaviour on synthetic pages. Measurement against real gold pages is not built yet.

## General technique

Hold out a set of hand-checked pages per collection, with ground truth for split, angle, orientation, content mask and transcription. Score each step by its own error, and score the whole chain by character and word error rate of the downstream readers on each variant compared with the unprepared scan. A step is kept only if it helps recognition, or at least does not hurt it while making review easier. Use the same set to calibrate every threshold and record that it was measured.
Source: general knowledge

## Settings in general terms

The gold set must be large enough to show differences between variants, and should cover each page situation that occurs in the collection. Tolerances for geometry depend on the physical sizes involved (a split error matters relative to the gutter margin). Recognition comparisons should use the same pages and readers for every variant.
