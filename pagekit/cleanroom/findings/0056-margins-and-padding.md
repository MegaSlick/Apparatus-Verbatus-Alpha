# Finding: kept photographed margin and added blank padding are separate controls

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

By default a prepared page should keep the available source around the writing, including paper edges where notes may sit. Separately, a person may want blank padding added around the page for readers that work better with a border. Mixing the two means a person cannot tell real paper from added fill, and excluding surrounding source should be a visible, explicit act.

## Pagekit's observed behaviour

Already: a margin in millimetres is added around the content box, held to the page box plus an allowance; anything outside the page or the source is filled with the measured paper colour; a blank page keeps its whole page box.

Differently: one margin setting both keeps photographed paper and, where it passes the paper, adds fill; there is no separate padding control, and the default trims to content plus margin rather than retaining all available source.

Not yet: separate controls for retained source and added padding; a default that keeps the available source; a visible mark of manually excluded surround; a record of which output regions are real source, mixed and pure fill.

## General technique

Describe the output in two steps: first the source domain kept (a polygon in source coordinates, defaulting to the whole page region), then the canvas, which may extend beyond it by a padding amount filled with a neutral value. Padding changes the canvas size, not the content scale. The review shows the excluded source, so exclusion is never hidden.
Source: general knowledge

## Settings in general terms

- Retained margin: default all available source; any reduction set by the person.
- Padding: a separate amount, default none, set in pixels or as a share of the page size.
- Fill value: neutral, measured from the paper, recorded.
