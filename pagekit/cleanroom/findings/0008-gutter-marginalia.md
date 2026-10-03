# Finding: writing that runs into the fold or sits in the gutter margin

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Register entries often run into the fold, and clerks wrote marginal notes, names and corrections in the inner margin. A cut placed exactly on the fold, or tight to the main text block, can slice these off one page or give them to the wrong page. The lead's document asks for each page crop to extend a configurable distance past the cut so that writing near the fold is not lost, and counts any ink component lost from either page as a split failure.

## Pagekit's observed behaviour

This is what pagekit's crop check is built for. Ink inside the page but outside every crop is reported as discarded, and writing that runs across a crop edge is reported as cut at that edge; either sends the page to review. Pagekit does not widen crops or propose an overlap; that is not built yet.

## General technique

Treat the cut as a boundary for ownership, not for pixels. Each page crop extends past the cut by an overlap, so a stroke that crosses the fold appears whole on at least one page. Better still, assign ink components rather than columns: a connected component that straddles the cut goes to the page holding most of it, and that page's crop is widened to contain it. After cropping, verify against the uncropped frame that no ink component was lost from both pages.
Source: general knowledge

## Settings in general terms

The overlap depends on how far writing typically crosses the fold in the series, in physical units, so it scales with resolution. The size above which a component counts as writing rather than a speck depends on the smallest meaningful mark (a dot or an abbreviation stroke), not on print character size.
