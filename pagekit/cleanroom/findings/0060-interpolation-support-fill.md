# Finding: declared interpolation, source support and fill regions

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Interpolation reads a small neighbourhood of source pixels for each output pixel. Near the edge of the selected region, that neighbourhood may reach across the cut into the neighbouring page or outside the source. In a tiled renderer, the extra border each tile reads (its halo) can pull in writing from outside the selected region. Some output pixels are fully from the source, some mix source and fill, and some are pure fill.

## Pagekit's observed behaviour

Already: quarter turns are exact; small rotation uses bicubic interpolation; shrinking uses area averaging; everything outside the page's polygon, including outside the source, is filled with the paper colour; the chain is stored as plain parameters.

Differently: the reports read do not describe whether a kernel near the cut can read source pixels from the neighbour's side, nor do they classify output regions.

Not yet: a declared operation order, working precision and boundary-fill rule; a classification of output regions as fully supported, mixed or pure fill, derived from the affine domain and the kernel's footprint; a guarantee that no kernel or tile border reads writing outside the selected domain.

## General technique

Each interpolation kernel has a known footprint. An output pixel is fully supported when its whole footprint, mapped into the source, lies inside the selected domain; pure fill when none of it does; mixed otherwise. For affine maps this classification follows from the domain polygon shrunk or grown by the footprint, without building a per-pixel array. Source samples outside the domain are treated as absent (masked) rather than read. Tiled output must equal whole-image output; this needs testing only when a tiled renderer exists.
Source: Wolberg, Digital Image Warping, IEEE Computer Society Press, 1990

## Settings in general terms

- Kernel and reduction filter: declared per page and recorded.
- Footprint margin: follows from the kernel's support and the scale factor.
