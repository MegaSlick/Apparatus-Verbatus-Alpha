# Finding: a single page with a strip of the neighbouring page in the frame

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

A frame meant to hold one page also shows a strip of the facing page, or of the next leaf, along one side. Writing on that strip runs off the frame edge. The strip must be cut away, but the cut must not take writing from the real page. The document records that its reference tool treats this as its own layout kind (one page plus off-cut), detected either from a fold line near one side of the frame or from content that touches the frame border on one side only.

## Pagekit's observed behaviour

Pagekit counts a neighbouring page in the frame as page, and its ink as discarded when it lies outside the crop, so such a frame is sent to review. This is noted in its limits. It does not detect the strip or propose a cut; that is not built yet.

## General technique

Two cues. First, a fold or page-edge line close to one side of the frame, found as in gutter detection; the cut follows that line and the opposite frame edge bounds the page. Second, content touching the frame border: writing that is cut by the frame edge on one side only belongs to a neighbour, so the cut goes into the first clear gap in from that side, between the cut-off writing and the page's own text block. If writing touches both sides, or the strip of cut-off writing is wider than a plausible neighbour strip, leave the page uncut and send it to review.
Source: general knowledge

## Settings in general terms

How close to the side a line must be to count as a neighbour edge depends on the framing of the series. The width of the border band examined for cut-off writing depends on the resolution and should be thin. The largest plausible neighbour strip, as a share of the frame width, should be measured on the collection.
