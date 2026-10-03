# Finding: a page or a single leaf that is upside down

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

A page is the right way across but rotated a half turn. In bound registers the facing pages of one spread can be captured inconsistently, and an inserted or loose leaf may be flipped while its neighbours are upright. The lead's document therefore asks for the half-turn check to run per page, after the spread is split, rather than once per image. The reference tool offers only a manual rotation per image, shared by both halves of a spread, so it cannot express one upright half and one flipped half.

## Pagekit's observed behaviour

Not built yet. Pagekit does not detect a flipped page. An upside-down page passes through the crop check like any other; nothing in its report would show the flip.

## General technique

Once lines are known to run across the page, upright and flipped differ only in asymmetries of the writing. Useful published cues are the balance of ascenders against descenders around the core height band of each line (Latin-script text has more mass above the core band than below it in most hands), and the alignment of line starts: entries are left-aligned and marginal names sit at the left, so the ragged edge of the text block is normally on the right. Each cue gives a vote with a strength; combine them and report a confidence. Where they are weak, a recognition engine or vision-language model can be asked to read both rotations, and the rotation with clearly better recognition confidence wins. Low confidence goes to review.
Source: R. S. Caprari, an algorithm for determining whether a text page is up or down, Pattern Recognition Letters, 2000; general knowledge

## Settings in general terms

The core band and ascender and descender zones should be measured per line from the writing itself, not fixed, because hands vary in size and slope. The confidence needed to accept a flip should be measured on a labelled set that includes flipped leaves, and should favour sending a page to review over silently rotating it, since a wrong half turn destroys the page for every downstream reader.
