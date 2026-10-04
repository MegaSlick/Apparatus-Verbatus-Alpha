# Finding: wide frames that are not simply two pages stay visible and pending

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A wide capture may be a two-page spread, a single page with a strip of its neighbour, an insert or slip laid over a page, or a fragment of another leaf. Forcing every wide frame into exactly two pages can split an insert, drop it, or invent a page.

## Pagekit's observed behaviour

Already: the page count is decided from evidence (fold, gap) rather than proportions; when cues disagree or none is strong, the frame is one page with a flag; a neighbour strip is flagged by side and left to the page box, which excludes it where it can be found and flags it where not.

Differently: the only outcomes are one page or two; an insert or overlay has no representation.

Not yet: a pending or unsupported arrangement outcome that keeps the whole capture visible for a person; a way to record an insert, neighbour fragment or overlay as such; left and right labels described as positions in the upright capture rather than as recto or verso.

## General technique

Model the arrangement as an open set of regions with roles, not a fixed count: primary page, neighbour fragment, insert, overlay, unknown. Automatic analysis may propose regions and roles; anything outside the supported subset is kept whole and marked pending rather than simplified. Physical layout analysis literature treats region segmentation and role labelling as separate steps, which makes this natural.
Source: Nagy, Twenty years of document image analysis in PAMI, IEEE Transactions on Pattern Analysis and Machine Intelligence, 2000

## Settings in general terms

- The supported subset: kept small at first (single pages and clearly separable spreads), widened by explicit decision.
- When a frame counts as uncertain: should depend on the same evidence strengths used by the split detector, measured on hand-checked frames.
