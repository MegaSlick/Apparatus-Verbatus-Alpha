# Finding: ruled lines and long straight marks on the page

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Registers are often ruled, with lines across the page, column rules and a margin rule. Rules are ink but not writing. They mislead skew estimation, can join unrelated words into one large component, and can be mistaken for text by a text-line test or for debris by a garbage test. The document records that its reference tool tells text from slightly skewed rules by counting local thickness maxima along a candidate line: a rule is one long thin stroke with few such maxima for its length.

## Pagekit's observed behaviour

Not built yet. Pagekit counts rules as ink like any other dark mark; a rule crossing a crop edge counts as ink cut at that edge.

## General technique

Detect rules by shape: long, thin, nearly straight components or runs, found by morphological opening with a long thin line element in each direction, or by a Hough transform. A distance-transform test separates rules from writing: along writing, the distance to the background has many separate local maxima (one per stroke); along a rule it is nearly constant. Keep rules in the content region (they are part of the page) but exclude them from skew estimation and from line segmentation.
Source: A. Meijster, J. B. T. M. Roerdink, W. H. Hesselink, "A general algorithm for computing distance transforms in linear time", Mathematical Morphology and its Applications to Image and Signal Processing, 2000; general knowledge

## Settings in general terms

The minimum length of a rule depends on the page width and the column layout. The thickness limit depends on pen width and resolution. The density of thickness maxima that marks writing depends on the writing size.
