# Finding: a page scanned on its side, a quarter turn from upright

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

A frame is stored sideways: the writing runs up or down the image instead of across it. This happens with microfilm frames, with camera captures where the operator turned the book, and with single leaves fed in the wrong way. Every later step (splitting a spread, deskew, finding content) assumes the writing runs roughly across the image, so the page must be put upright first. The document records that its reference tool has no automatic orientation at all: the user sets it by hand, and the setting is shared by both halves of a spread.

## Pagekit's observed behaviour

Not built yet. Pagekit reads the master in its stored pixel grid and ignores any rotation recorded in metadata. It does not detect or change orientation. Its ink checks still run on a sideways page, but a sideways page gives no orientation warning, and its gutter search (which looks for a low-ink run of columns near the middle) would look in the wrong direction on a sideways spread.

## General technique

Lines of writing make the ink profile across rows strongly peaked (dense bands of ink alternating with gaps), while the profile across columns is much flatter. Compute the ink projection in both directions on a binarised, reduced version of the page and compare how sharply each varies; the direction with the sharper, more periodic profile is the direction the lines run. Line-spacing periodicity and connected-component shapes (components are usually wider than tall in running cursive) are useful second cues. Report a rotation together with a confidence, and send the page to review when the two directions score too close to call rather than guessing.
Source: H. S. Baird, "The skew angle of printed documents", Proc. SPSE Symposium on Hybrid Imaging Systems, 1987; general knowledge

## Settings in general terms

The working resolution should be low enough to be fast but should keep line gaps visible, so it depends on the expected height of a line of writing. The margin by which one direction must beat the other before the result is trusted should be measured on real register pages, since dense or ruled pages flatten the difference. Ruled lines and the dark page edge should be masked before scoring, because long straight marks bias the profile toward their own direction.
