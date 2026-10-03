# Finding: a page whose skew, content size or margins are unlike the rest of the volume

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Within one volume, most pages have similar skew, similar content size and similar margins. A page that differs strongly is often a detection error: a wrong split, a content box that swallowed a shadow, or a skew locked onto a rule. The document records that its reference tool marks such pages in its page strip using a mean and spread over the volume, and lets the user sort pages by how far they deviate.

## Pagekit's observed behaviour

Not built yet. Pagekit checks one master at a time and keeps no statistics across a volume.

## General technique

Collect each per-page measurement across the volume, compute a robust centre and spread (median and median absolute deviation are less disturbed by the very outliers being sought than mean and standard deviation), and flag pages that lie far from the centre. Use a floor on the spread so that a very uniform volume does not flag tiny differences. Offer the review queue sorted by deviation. Only flag when enough pages exist for the statistics to mean anything.
Source: P. J. Rousseeuw and C. Croux, "Alternatives to the median absolute deviation", Journal of the American Statistical Association, 1993; general knowledge

## Settings in general terms

The distance at which a page is flagged depends on how variable a clean volume is, measured per collection. The spread floor depends on the measurement precision of the detector. The minimum number of pages depends on how stable the statistic is for small volumes.
