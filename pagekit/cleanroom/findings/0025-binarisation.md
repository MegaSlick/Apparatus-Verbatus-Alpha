# Finding: black-and-white output of faded, uneven handwriting

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Turning a page into pure black and white throws away tone. On early-modern registers written in iron-gall ink, ink fades unevenly to brown or yellow, and the hairlines of a quill are much lighter than the main strokes. The document records that its reference tool defaults to black-and-white output with a global threshold, offers two local thresholds, and smooths the image before thresholding. Its own notes judge any black-and-white output harmful for recognition: a global threshold drops hairlines, joins, superscript abbreviation marks and faded entries, and promotes show-through to black. The lead's document keeps a binary image only as a diagnostic and layout artefact, never as reader input.

## Pagekit's observed behaviour

Pagekit binarises internally, with Otsu's global threshold, only to measure ink for its crop checks; it produces no output image. Its limits note that a global threshold can move faint ink to the paper side and stains to the ink side. Output binarisation is not built yet.

## General technique

When a binary image is needed (for layout analysis or line segmentation), threshold locally on an illumination-corrected grey image. Sauvola's method sets each pixel's threshold from the local mean and local standard deviation, lowering it in flat regions; Wolf and Jolion's variant normalises by the image's contrast range and works better on low-contrast pages; Niblack's method is the older basis of both. Otsu's global method is the baseline. A light edge-preserving smoothing before thresholding (for example a local polynomial or Savitzky and Golay filter) reduces paper grain and compression noise. Keep the grey or colour image for recognition.
Source: J. Sauvola and M. Pietikäinen, "Adaptive document image binarization", Pattern Recognition, 2000; C. Wolf, J.-M. Jolion, F. Chassaing, "Text localization, enhancement and binarization in multimedia documents", Proc. International Conference on Pattern Recognition, 2002; W. Niblack, An Introduction to Digital Image Processing, Prentice Hall, 1986; N. Otsu, "A threshold selection method from gray-level histograms", IEEE Transactions on Systems, Man, and Cybernetics, 1979; A. Savitzky and M. J. E. Golay, "Smoothing and differentiation of data by simplified least squares procedures", Analytical Chemistry, 1964

## Settings in general terms

The local window should span several strokes and at least a letter height, so it depends on writing size and resolution. The sensitivity factor depends on the contrast between faint ink and paper, measured on faded pages. Any smoothing window depends on resolution and must stay below stroke width so it does not erase hairlines. A clamp on a global threshold protects against pages with almost no ink, and should depend on the measured histogram, not a fixed range.
