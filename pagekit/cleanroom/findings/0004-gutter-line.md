# Finding: splitting a spread at the fold or gutter

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

A two-page spread must be cut at the fold. The fold usually shows as a thin dark vertical line (the spine edge or a crease), or, in bound registers, as a soft dark shadow that grows gradually darker toward the binding. It may lean slightly, because the book was not square on the scanner. Book edges and the outer edges of the pages also form dark vertical lines and must not be mistaken for the fold.

## Pagekit's observed behaviour

Pagekit checks a declared split, but does not find one. It takes the ink share of each column, takes the lowest-ink run of columns in a window around the middle as the gutter, and flags a split that lies outside that run by more than a tolerance proportional to the page width. Its own limits note that a dark gutter shadow (which reads as ink), a skewed spread or a page with no blank gutter can mislead this search. Proposing the split is not built yet.

## General technique

Two complementary detectors. First, a thin-line detector: enhance thin dark structures that are much taller than wide (a morphological black top-hat with an element wider than the line does this) and run a Hough transform restricted to near-vertical angles, so the fold may lean a little. Ignore candidates near the outer edges, which are page or book edges, and prefer the strong candidate nearest the middle. Second, a brightness-valley detector for soft shadows: smooth the column brightness profile and look for a broad minimum near the middle. Record which detector decided and its strength; disagreement goes to review.
Source: R. O. Duda and P. E. Hart, "Use of the Hough transformation to detect lines and curves in pictures", Communications of the ACM, 1972; J. Serra, Image Analysis and Mathematical Morphology, Academic Press, 1982

## Settings in general terms

The working resolution should keep the fold several pixels wide, so it depends on the scan resolution and the fold's physical width. The range of lean allowed depends on how square the books were placed, measured from the collection. The band near the outer edges in which candidates are ignored depends on how much backdrop and book edge the framing leaves. The minimum strength of a fold candidate should be relative to the page height, since a true fold runs most of the way down the page.
