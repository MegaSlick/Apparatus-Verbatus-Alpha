# Finding: a page that is slightly rotated

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

A page sits at a small angle, so its lines of writing slope across the image. Recognition models and line segmenters work best with level lines, and a skewed page wastes crop area. Skew is a per-page property: the two halves of a spread can lean differently. Handwriting makes estimation harder than print, because baselines wander and lines slope differently along the page.

## Pagekit's observed behaviour

Not built yet. Pagekit's crops are axis-aligned and rotation is not checked; its limits say so. A skewed page still gets its crops checked for discarded and cut ink, but no angle is measured.

## General technique

Projection-profile skew estimation. For each candidate angle, project the binarised ink onto rows after rotating or shearing by that angle; when the angle matches the lines, the profile alternates sharply between dense line bands and empty gaps. Score the sharpness, for example by the energy of the differences between neighbouring rows or by the variance of the profile, search coarsely over the plausible range on a reduced image and refine near the best angle. Trust the result only when the best score stands clearly above the typical score across the range; otherwise apply no rotation and flag the page. A second, independent estimator is a useful cross-check on handwriting: a Hough transform on a baseline map (ink smeared horizontally so each line becomes a band), or the median of the angles of chains of neighbouring components. When the two disagree beyond a tolerance, flag the page.
Source: W. Postl, "Detection of linear oblique structures and skew scan in digitized documents", Proc. International Conference on Pattern Recognition, 1986; H. S. Baird, "The skew angle of printed documents", Proc. SPSE Symposium on Hybrid Imaging Systems, 1987; D. S. Le, G. R. Thoma, H. Wechsler, "Automated page orientation and skew angle detection for binary document images", Pattern Recognition, 1994

## Settings in general terms

The search range depends on the worst skew seen in the collection. The refinement step depends on the angle at which a full page width of text moves by about one line's height, so it depends on page width and line spacing. The reduction used for the coarse search must keep line gaps visible, so it depends on line spacing and resolution. The margin by which the best score must beat the typical score, and the disagreement tolerance between estimators, should be measured on real pages. Very small angles may be snapped to zero to avoid a pointless resample.
