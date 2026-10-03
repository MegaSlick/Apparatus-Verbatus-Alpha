# Finding: skew estimation failing on blank pages, steep skew or mixed angles

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

The document records several cases where skew estimation cannot give a right answer: a blank or nearly blank page gives too little signal; a page skewed beyond the search range cannot be found; and a page whose regions lean at different angles (two columns, a marginal note at an angle, a pasted slip) yields only one of the angles, with low confidence. In each case its reference tool falls back to no rotation.

## Pagekit's observed behaviour

Not built yet. Pagekit does not estimate skew.

## General technique

Treat each as a reported outcome, not a silent default. A page with too little ink for a reliable profile gets no rotation and a "too little content" reason. A best angle at the edge of the search range means the true skew may lie beyond it: widen the range for that page or send it to review. For mixed angles, estimate locally (per region or per line) and compare; when local angles disagree, apply the dominant angle only if it covers most of the writing, and flag the page. Fall back to no rotation only with a recorded reason and a review flag.
Source: general knowledge

## Settings in general terms

The minimum ink needed for an estimate depends on the line count and writing size, measured on sparse pages. The region size for local estimates depends on the line length and the layout of the registers. How much of the writing the dominant angle must cover before it is applied is a value to measure.
