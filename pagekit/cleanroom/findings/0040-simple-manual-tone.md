# Finding: simple manual tone controls, off by default and fully defined

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The person wants simple contrast, levels and gamma controls early, to make faded microfilm easier to read, but the controls must not quietly destroy faint strokes. A friendly slider can clip shades to pure black or white, merge neighbouring levels, or change meaning depending on the order in which it is applied.

## Pagekit's observed behaviour

Already: the grey tone view applies one smooth, monotone, non-clipping tone curve after flattening, records its settings, and is tested so that no faint stroke or hairline disappears and no two levels below the paper merge by clipping.

Differently: the tone curve is fixed in shape and tied to the tone view; it is not a manual control on the main page, and flattening always comes with it.

Not yet: manual levels, contrast and gamma controls as separate candidates on the main page, off by default; a comparison of faint and protected marks before and after.

## General technique

Point-wise tone mapping (levels, gamma, contrast) is a function from input level to output level. To be safe for evidence it should be monotone, its clipping should be stated and shown (how many pixels reach the ends), its working precision should be higher than the output so rounding does not create merged levels, and its place in the chain (before or after grey conversion, before or after resampling) should be fixed and recorded. Each setting produces a new, reproducible candidate rather than altering an accepted page. Automatic background correction, despeckling, sharpening, thresholding and bleed-through removal are separate, later profiles needing their own evaluation.
Source: Gonzalez and Woods, Digital Image Processing, Pearson, fourth edition, 2018 (intensity transformations and histogram processing)

## Settings in general terms

- Default: off; the geometry-only page is the baseline.
- Black and white points: set by the person, with the share of clipped pixels shown; a sensible guard depends on how many pixels hold faint ink.
- Gamma or contrast strength: set by the person, bounded so the mapping stays monotone.
- Working precision: higher than the output depth.
- Order in the chain: fixed and recorded with the page.
