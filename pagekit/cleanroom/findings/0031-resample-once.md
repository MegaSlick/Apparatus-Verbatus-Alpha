# Finding: rotating, cropping and scaling without repeated blur, and the output resolution

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Orientation, split, deskew, crop and scaling each change geometry. Producing a new image after each step resamples the pixels again and again, and every resample blurs faint ink a little more. Output resolution matters too: the document records that its reference tool writes at a high default resolution, which upsamples most archival scans without adding information, inflates files and slows processing. The lead's document asks to keep the native resolution, cap it, never upsample, and let each reader model have its own target size, since those models resize to a pixel budget internally and tight cropping is the main lever.

## Pagekit's observed behaviour

Not built yet. Pagekit does not transform or write images. It flags a master whose short side is below a minimum or whose resolution is missing.

## General technique

Keep geometry as a composition of transforms (orthogonal rotation, page polygon, small rotation, crop, scale), each stored with its parameters, and apply only the composed transform to the original image, once, at the end. Analysis steps may render their own reduced working copies, but outputs always come from the original. Downscale with an area-averaging (box) filter, which avoids aliasing; use a high-quality kernel for any rotation; never upsample beyond the capture resolution.
Source: general knowledge

## Settings in general terms

The output size for each reader depends on that reader's internal input size, and should be set per reader in configuration and tuned by recognition results. The cap on resolution depends on the smallest strokes that must stay visible. The resampling kernel depends on whether the step shrinks or rotates.
