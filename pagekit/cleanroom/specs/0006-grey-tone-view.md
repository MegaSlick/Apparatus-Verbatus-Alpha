# Spec 0006: the grey tone view

Written by the host before any code, from finding reports 0023, 0024, 0025, 0030 and
0032, the papers they cite, and a published study of preprocessing for vision-language
readers of historical handwriting. Nothing in it comes from ScanTailor, ScanTailor
Advanced or any other GPL program.

## Purpose

Some readers see a page in grey and measure ink against one global level: the reader
model that establishes the text is shown a grey render, and the ink map thresholds the
page once. Uneven lighting (a gutter shadow, vignetting, stains) and faded brown
iron-gall ink defeat both. The other readers, which were trained on raw colour scans,
should keep the page as scanned.

So this slice builds a **grey tone view**: a function that takes a page image (grey or
colour) and returns a grey image of exactly the same size, pixel for pixel in the same
place, with the lighting evened out and faint ink lifted, and nothing else. It never
moves a pixel, never binarises, and is never applied to the master or to the prepared
page that other readers see. It is a view, made on request.

Evidence on what helps: published work on vision-language readers of historical
handwriting found that binarisation, denoising and strong local contrast equalisation
raised the error rate, and that only mild sharpening helped (Farazi et al., 2026,
arXiv:2608.22366). So the view is deliberately gentle.

## Steps, in order

1. **Grey conversion.** For a grey source, use it as it is. For a colour source, form
   grey from the source's channels by a rule chosen by setting: luminance (the default),
   or the minimum of the three channels, or a single channel. The minimum and the blue
   channel often separate brown ink from yellowed paper better than luminance (finding
   0032); which one is right is to be measured, so the setting names the choice and the
   view records it.
2. **Flat-field correction** (finding 0023). Estimate the paper background without the
   ink, on a reduced copy, with a filter much larger than any stroke (a large median, or
   a large morphological closing, which removes dark strokes and keeps the slowly
   varying paper). Bring the estimate back to full size smoothly. Divide the grey image
   by the background so the paper becomes even, and scale so that paper lands at a set
   level just below white, never at pure white. Guards:
   - too few paper samples (a page almost covered in writing or in shadow): no
     correction, and the view says so;
   - a sharp edge in the background (a fold crease, the edge of a water stain) that a
     smooth estimate cannot follow: the local filter handles it, and halos are tested
     for.
   (Gatos, Pratikakis and Perantonis 2006; Lu, Su and Tan 2010.)
3. **Faint-ink lift.** A single smooth, monotone tone curve applied after flattening,
   which darkens mid-tones near the paper level more than it darkens ink that is already
   dark, so faint strokes gain contrast against the paper. It must be monotone (a darker
   input never becomes lighter than a lighter one) and must not clip: no two distinct
   input levels below the paper level may map to the same output level because of
   clipping, so hairlines and faint entries keep their shading. Strength is a setting.
4. **Mild sharpening** (optional, off by default). A small unsharp mask with a radius
   below the stroke width and a small amount. (Zuiderveld's contrast-limited local
   equalisation is not used: the study above found it hurt.)

Show-through attenuation (finding 0030) and colour balancing (finding 0024) are not in
this view. Contrast-limited local equalisation and binarisation are not in it.

## What the view records

Every call returns the image and a record: the steps applied with their settings, the
grey rule, whether flattening ran or was skipped and why, the estimated paper level
before and after, and a digest of the settings. The same input and settings give a
byte-identical image and record.

## Measures the tests use

Tests use synthetic pages built in the test: writing of known strokes, some at full
darkness and some faint (a few grey levels below the paper), hairlines one pixel wide,
on paper with a strong gradient (a gutter shadow) and a stain with a sharp edge, in grey
and in colour (brown ink on yellow paper).

- **Paper evenness**: the spread of paper levels across the page falls sharply after
  flattening.
- **Faint-ink contrast**: the contrast of faint strokes against the paper around them,
  divided by the paper's noise, rises.
- **No loss**: every stroke that is darker than its surrounding paper before is still
  darker after; no hairline disappears; no two input levels below paper merge by
  clipping.
- **No movement**: the view has the input's size, and the darkest point of every stroke
  stays at the same pixel.
- **Gentle**: on an already even page with dark ink, the view changes the image only a
  little (a bound on the mean absolute change).
- **Guards**: a page almost covered in ink skips flattening and says so; a sharp stain
  edge leaves no halo wider than a set distance.
- **Colour rule**: on brown ink over yellow paper, the minimum-channel rule gives higher
  faint-ink contrast than luminance; both are available.
- **Determinism and speed**: byte-identical on repeat; a 3000 by 4500 page in a few
  seconds with Pillow and the standard library.

## Command line

`python -m pagekit tone --in PAGE --out VIEW.tif` writes the view as lossless TIFF and
prints its record. `prepare` gains an option to also write a tone view beside each
prepared page, off by default.

## Not in this slice

Using the view anywhere in the main pipeline (that is integration work outside pagekit,
and a decision for the lead); colour balancing; show-through attenuation; dewarping;
binarisation of any kind.
