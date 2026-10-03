# Finding: keeping pictures in tone while thresholding text (mixed output)

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Mixed output keeps illustrations, seals and photographs in continuous tone while thresholding the text around them. It depends on an automatic picture detector. The document records that its reference tool detects pictures as regions of strong texture, and that on registers the detector misfires: dense cursive, heavy stains, seals and decorated initials are taken for pictures, so parts of a page stay grey while the rest is thresholded, inconsistently from page to page.

## Pagekit's observed behaviour

Not built yet. Pagekit produces no output image.

## General technique

Text and graphics separation by texture and component statistics is a standard layout-analysis step. For manuscript registers, avoid mixed output entirely: keep the whole page in tone. If a region must be treated differently (for example a seal or a stamp to mask), mark it as a zone, by hand or by a dedicated detector, and record it, rather than relying on a general picture detector.
Source: general knowledge

## Settings in general terms

If picture detection is used at all, its sensitivity depends on writing density and stain patterns, and should be measured per collection. The default for registers should be off.
