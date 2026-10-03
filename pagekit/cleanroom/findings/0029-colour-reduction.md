# Finding: colour segmentation and posterisation that flatten ink shading

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Some outputs reduce colour: painting each ink component with one flat colour, or reducing the image to a few colour levels. The document records that its reference tool offers both, off by default, and its notes judge both harmful for manuscripts: stroke shading, which carries pressure and ductus, is lost, and at few levels faint ink collapses into paper or into black, the same failure as global thresholding.

## Pagekit's observed behaviour

Not built yet. Pagekit produces no output image.

## General technique

Colour quantisation (popularity or median-cut palettes) and component-wise colour flattening are compression techniques. For recognition, keep the full tonal range. If ink colour needs to be separated (for example to tell a later annotation from the original entry), keep it as an analysis layer alongside the full image rather than replacing the image.
Source: P. Heckbert, "Color image quantization for frame buffer display", Proc. SIGGRAPH, 1982

## Settings in general terms

No setting for reader images: off. For an analysis layer, the number of ink colours depends on how many inks the collection shows.
