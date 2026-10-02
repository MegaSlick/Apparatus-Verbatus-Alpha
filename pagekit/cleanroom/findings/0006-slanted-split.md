# Finding: a split line that leans because the book was not square

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

When a book is placed slightly crooked, the fold is not a vertical column of pixels but a slightly slanted line. A vertical cut then clips the top of one page and the bottom of the other, or leaves a wedge of the neighbour in each half. Each half may also be skewed by a different amount.

## Pagekit's observed behaviour

Not built yet. Pagekit's split is a single column and its crops are axis-aligned boxes. Its limits note that a skewed spread can mislead the gutter search and that rotation is not checked. A slanted fold would show up only indirectly, as ink cut at a crop edge or ink discarded outside the crops.

## General technique

Represent a cut as a line through two points rather than as a column, so it can lean. Each page region is then the polygon between the outer page edge and the cut. A line found by a Hough transform restricted to near-vertical angles already gives a leaning cut; a cut found in a deskewed frame is mapped back through the inverse rotation or shear and becomes a leaning line in the original. Deskew is measured per page after the split, because the two halves can lean differently.
Source: general knowledge

## Settings in general terms

The largest lean accepted for a cut should match the largest skew seen in the collection. When the cut is moved to a page's own coordinate frame, the polygon should be kept, not only its bounding box, so that later steps can mask the neighbour's wedge.
