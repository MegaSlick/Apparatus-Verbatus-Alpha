# Finding: a reviewed grey page wanted as a main prepared page, not only as a side view

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The person preparing microfilmed registers wants to choose, page by page or for a named batch, between keeping the source colour mode and producing a grey page, and wants that choice in the very first usable version of the tool. Many microfilm captures arrive as colour files although they carry no colour, so a grey page is often the natural main page. The grey page must have exactly the same geometry as the source-mode page, so the two can be compared side by side, and the choice must be a reviewed decision rather than an automatic one.

## Pagekit's observed behaviour

Already: the prepared page keeps the source colour mode (a colour scan gives a colour page, a grey scan a grey page), and pagekit never turns a page into black and white. The grey tone view can be written beside each prepared page on request; it has the same size and pixel positions as the prepared page.

Differently: grey exists only as a side view for particular readers. It always flattens the lighting and lifts faint ink, so it is not a plain grey conversion of the page; it is never the main prepared page, and the prepared page itself cannot be made grey.

Not yet: a plain, reviewed grey conversion offered as the main output; a per-page or per-batch choice between source mode and grey recorded with who chose it and why; a side-by-side comparison of the two candidates in the review sheet.

## General technique

Treat source-mode and grey as two candidates rendered through the same geometry from the same original, so that they differ only in their sample values. Grey is produced by a declared weighted sum of the colour channels (a luminance rule) or another declared single-channel rule, and the rule is recorded with the page. Which candidate becomes the main page is an explicit, recorded choice. This is the ordinary separation between geometric resampling and point-wise intensity mapping found in image-processing textbooks.
Source: Gonzalez and Woods, Digital Image Processing, Pearson, fourth edition, 2018 (intensity transformations and colour models)

## Settings in general terms

- The grey rule: chosen by what distinguishes ink from paper in the collection; recorded with every page.
- The default output mode: should follow the person's choice for the batch, with source mode as the safe default until a choice is made.
- Whether a grey main page needs review: should depend on whether the source carries meaningful colour (see the finding on grey losing evidence).
