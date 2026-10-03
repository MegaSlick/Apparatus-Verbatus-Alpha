# Finding: mapping coordinates on a prepared image back to the archival scan

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Recognition and transcription run on prepared images: rotated, cropped, scaled, perhaps dewarped. Line positions in a transcription then refer to the prepared image, not to the archival scan that a citation must point to. The lead's document asks for the transform from the original scan to each variant to travel downstream, so transcription coordinates can be mapped back for citation and re-checking.

## Pagekit's observed behaviour

Not built yet. Pagekit produces no prepared image. Its crop boxes are stated in the master's stored pixel grid, which is the frame any later mapping would return to.

## General technique

Record the full geometric chain for every output: the source file and its hash, then each transform with its parameters (orthogonal rotation, page polygon, small rotation with its centre, crop offset, scale), and any non-linear warp as an invertible mapping or a sampled grid. Provide forward and inverse mapping of points and polygons. Store the chain in the manifest beside the output's hash, in a form that does not depend on the tool's internals.
Source: general knowledge

## Settings in general terms

The precision of stored parameters must keep the round-trip error well under a pixel at the original resolution. A warp grid's spacing depends on how sharply the warp varies.
