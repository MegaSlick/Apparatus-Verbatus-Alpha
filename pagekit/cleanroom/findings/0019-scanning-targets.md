# Finding: colour checkers, rulers and call-number slips in the frame

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Archival captures often include a colour checker, a grey scale, a ruler, or a slip with the archive's call number, placed beside or on the page. These are not part of the document. If they are counted as content they widen the crop; if the page-edge finder sees them they confuse it; and their strong colours bias any background or colour estimate. The lead's document asks for them to be detected and excluded as a named class. The document records that its reference tool has no such class.

## Pagekit's observed behaviour

Not built yet. A target inside the page area counts as ink; if it lies outside the crop it is reported as discarded ink and the page goes to review.

## General technique

Detect targets as objects with properties writing never has: regular grids of saturated, uniform colour patches (colour checkers); evenly spaced short tick marks along a straight bar (rulers); a rectangular region of different paper colour with printed type (slips). Template matching against known target designs used by the archive is reliable when the set of targets is small and fixed. Mask detected targets out of content, page-edge and colour estimation, and record their position, since a known colour checker can later calibrate colour.
Source: general knowledge

## Settings in general terms

Template sizes depend on the resolution and the physical size of the targets the archive uses. The saturation and uniformity tests depend on the camera and lighting of the capture campaign. The list of known targets belongs in per-collection configuration.
