# Finding: ruled lines, page edges and shadow bands that mislead skew estimation

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Long straight dark features dominate a projection profile: ruled lines of a register, the dark edge of the page or book, the backdrop around the paper, and horizontal shadow bands along the top or bottom of a page. If they are not removed, the estimated angle is the angle of the ruling or the edge, not of the writing, and a dark band can swamp the score entirely.

## Pagekit's observed behaviour

Not built yet for skew. Pagekit trims border rows and columns that are nearly all dark as scanner backdrop; its limits note that a shadow which does not fill a whole row or column is counted as page, and its ink as ink.

## General technique

Measure skew only on writing. Restrict estimation to the content region, inside the page edge. Remove very large dark blobs first: a morphological opening with an element much longer than any letter keeps only wide bands and long rules; morphological reconstruction from those survivors recovers each whole connected band, which is then subtracted from the ink. Long thin straight components (rules) can also be removed by their shape: long, thin and nearly straight compared with any piece of writing. Ruled lines may then be used, separately, as their own skew estimate and compared with the writing's.
Source: L. Vincent, "Morphological grayscale reconstruction in image analysis: applications and efficient algorithms", IEEE Transactions on Image Processing, 1993; general knowledge

## Settings in general terms

The element used to find bands must be much longer than the widest word and thicker than a pen stroke, so its size depends on the writing size and the resolution. The shape test for rules depends on the ratio of length to thickness, scaled by the resolution. Whether a removed rule set is used as a second skew estimate is a design choice to test on ruled registers.
