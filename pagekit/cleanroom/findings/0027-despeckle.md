# Finding: removing specks without removing dots, accents and abbreviation marks

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Scans carry specks from dust, paper fibres and noise. Handwriting also carries small marks that matter: i-dots, macrons and tildes of Latin and French abbreviations, superscript letters, cedillas and accents, punctuation and abbreviation points, and isolated numerals. The document records that its reference tool removes small connected components that are not close enough to a larger one, with an adjustable strength, and counts vertical distance as larger than horizontal distance so marks sitting in a line are favoured. Its notes judge this harmful at normal and strong settings: marks well above or below the line lose their anchor and are erased. The lead's document forbids despeckling on any image sent to a reader model.

## Pagekit's observed behaviour

Pagekit applies a small median filter to its internal ink map before counting, to stop isolated specks raising crop flags. Its limits note that this also removes strokes about one pixel thick. It produces no output image, so output despeckling is not built yet.

## General technique

Connected-component noise removal: label components, and remove those that are both small and far from any substantial ink. Proximity should allow for marks above and below the line (dots, superscripts, macrons), so the neighbourhood around writing must reach at least about one line height in every direction. A safer approach for manuscripts is to remove only specks that lie outside the content region, or that are isolated in large empty areas, and to keep everything near writing. On reader images, leave specks alone; models tolerate noise better than missing diacritics.
Source: general knowledge

## Settings in general terms

The size below which a component is a speck depends on the smallest meaningful mark at the scan resolution. The reach within which a small mark counts as belonging to writing depends on line height and the height of superscripts above the line. Whether despeckling runs on any given image type should be decided by recognition results, defaulting to off for reader images.
