# Finding: a blank or nearly blank page

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Volumes contain blank pages, blank versos and pages with a single word or stamp. Detectors that look for structure have nothing to work with: skew estimation has no signal, content detection returns nothing, and a threshold chosen on a page of bare paper splits paper grain into ink and paper. The document records that its reference tool treats an empty content box as legal (meaning a blank page), still gives the page a margin and size, and writes an all-white page when the content area is empty.

## Pagekit's observed behaviour

Pagekit handles this case: after the threshold is computed on the page, if its dark and light sides differ by less than a set contrast the page is treated as carrying no ink, and a blank page raises no crop flags. This is pinned by its tests. Downstream handling (no rotation, an output for the blank page) is not built yet.

## General technique

Decide blankness first, from contrast and ink amount, and carry it as an explicit result. A blank page gets no rotation, a content box of none, and a reason recorded. For output, keep the page (as a real image of the paper, not a synthetic white page) so the sequence of pages stays complete and a reviewer can confirm it is blank. A page with only a few marks is not blank: its marks must survive.
Source: general knowledge

## Settings in general terms

The contrast below which a page is blank depends on the paper and the capture, and should be measured on real blank pages, including stained ones and ones with show-through. The smallest amount of ink that makes a page not blank depends on the smallest meaningful mark.
