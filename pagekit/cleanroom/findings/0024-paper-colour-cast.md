# Finding: yellowed or tinted paper that should be balanced to neutral

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Old paper is yellow or brown, and capture lighting adds its own tint. The document records that its reference tool has no white balance: its illumination correction replaces brightness only and keeps the colour differences, so a yellow cast survives. The lead's document asks for a colour variant that balances the paper to neutral while keeping ink colour, because ink colour can distinguish hands and later annotations.

## Pagekit's observed behaviour

Not built yet. Pagekit reduces the master to grey and does not produce output.

## General technique

White balance against the paper. Estimate the paper colour from pixels classified as paper (after flat-field correction, so lighting gradients do not bias it), and scale each colour channel so that this paper colour maps to neutral. This is the grey-world or white-patch idea restricted to a region known to be neutral in reality. If the capture includes a known colour or grey target, calibrate from that instead. Apply the same per-channel gains to the whole page so ink colour relationships are kept.
Source: G. Buchsbaum, "A spatial processor model for object colour perception", Journal of the Franklin Institute, 1980; general knowledge

## Settings in general terms

The paper-pixel selection depends on a reliable ink and paper split. Whether gains are global per page or vary smoothly across the page depends on whether the tint varies with lighting. The target neutral level depends on what the downstream readers were trained on, and should be judged by recognition results.
