# Finding: review detail must come from the actual final page, at native detail

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

While the person moves a control, quick rough previews are fine. But when the person decides to accept a page, what they inspect must be the very page that will be exported, at full resolution where they zoom in on a boundary, the gutter or a protected mark; not a separately processed approximation that might differ.

## Pagekit's observed behaviour

Already: the prepared page is made once from the original; the review sheet shows a small preview of the upright original with the cut and boxes drawn, and a small preview of each prepared page.

Differently: the previews are small lossy copies meant only for looking; the sheet never shows full-resolution detail, so a faint stroke at an edge cannot be checked there.

Not yet: native-resolution detail of the final page on demand; source-relative detail around boundaries, the gutter and protected marks; cancelling obsolete renders while the person adjusts; draft previews marked as drafts.

## General technique

Keep two rendering paths with different promises: a cheap draft path for interaction, visibly marked as a draft, and a final path that renders the candidate once at its output resolution. Freeze that final render (for example as a verified staging file) and serve every review view, overview or detail, from it, so what is seen is what is accepted. Render on request or after a short idle pause, and cancel work made obsolete by a newer change. Detail is computed on demand for the region viewed rather than for every tile.
Source: general knowledge

## Settings in general terms

- The idle pause before a final render: should depend on how long a final render takes on the target machine, so interaction stays smooth.
- Draft resolution: should depend on the display size, not on the source size.
- Which details are offered first: the page boundaries, the gutter and any marks the person has protected.
