# Finding: finding the physical edge of the paper inside the frame

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

The frame contains the scanner bed, a backdrop (dark or pale), the book edge and shadows around the actual sheet. Knowing where the paper is lets later steps ignore everything outside it and lets the output margin be clamped to the real page. Backdrops differ in brightness, and the page edge may be soft, curved or torn.

## Pagekit's observed behaviour

Pagekit finds the page area only by trimming border rows and columns that are nearly all dark. Its limits note that a pale backdrop, a scanner shadow that does not fill a whole row or column, or a neighbouring page in the frame are counted as page. Proper page-edge detection is not built yet.

## General technique

Page-frame detection. Binarise the frame (more than one threshold method can be tried, because backdrops vary), then walk inward from each side, row by row or column by column, while the scanline is almost entirely dark backdrop; the page edge is where that run of backdrop lines ends and stays ended for a while. For pale backdrops, use the edge in brightness or colour between backdrop and paper instead of darkness. When the expected paper size is known for the series, prefer the candidate whose width and height best match it. Published page-frame methods fit the frame from the text-line and component layout and remove marginal noise outside it.
Source: F. Shafait, J. van Beusekom, D. Keysers, T. M. Breuel, "Document cleanup using page frame detection", International Journal on Document Analysis and Recognition, 2008; K.-C. Fan, Y.-K. Wang, T.-R. Lay, "Marginal noise removal of document images", Pattern Recognition, 2002

## Settings in general terms

The share of dark pixels that makes a scanline backdrop depends on how clean the backdrop is. How many non-backdrop lines end the walk depends on the size of dust, tears and labels at the edge. An expected page size, if used, comes from the series' catalogue data, with a tolerance measured on the volume.
