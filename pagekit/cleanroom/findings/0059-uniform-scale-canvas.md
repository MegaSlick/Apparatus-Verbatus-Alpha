# Finding: deskew with expanded bounds and a uniform content scale

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Each leaf of a spread is levelled by its own angle. Rotating a page needs a larger canvas to keep its corners. When a page is also scaled, stretching it to rounded integer dimensions gives slightly different scales on the two axes, which distorts letter shapes a little; this should be avoided or stated, not described as exact.

## Pagekit's observed behaviour

Already: each page has its own skew, rotated about the page's centre; the page is made from the original in one resampling; pages are never enlarged; a rotated and shrunk page is sampled on a finer grid and block-averaged in one filter.

Differently: the reports read do not say whether a shrunk page's two axes keep exactly the same scale after rounding, or how the canvas is rounded.

Not yet: a stated rule of a mathematically uniform scale into a canvas rounded outward; or, for a simpler resize, a stated and tested bound on aspect error; a declared rotation sign and pivot for each leaf.

## General technique

Compute the transformed page's exact bounds, round the canvas outward to whole pixels, and place the content with one scale factor applied to both axes; the extra fraction of a pixel goes to the border. Stretching to exact rounded target dimensions instead applies two slightly different factors. Padding enlarges the canvas without touching the scale. Re-render always from the original decoded source, never from an earlier derivative.
Source: Wolberg, Digital Image Warping, IEEE Computer Society Press, 1990

## Settings in general terms

- Rotation sign and pivot: fixed and documented.
- Allowed aspect error, if a rounding resize path is offered: should depend on the smallest shape difference that matters to readers, and be stated with the path.
