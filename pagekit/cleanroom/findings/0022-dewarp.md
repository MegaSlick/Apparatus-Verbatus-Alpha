# Finding: a page curved near the binding, so lines bend

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

In tightly bound volumes the paper curves down into the gutter, so lines of writing bend near the fold and letters there are compressed. Deskew cannot fix this, since it rotates the whole page. The document records that its reference tool offers dewarping as an output option, off by default, with automatic, manual and margin-based modes, a page model of a curved surface between a top and a bottom curve, and a fall-back to no correction when the model is invalid. The lead's document keeps dewarping off by default and behind a flag, because automatic models built from irregular handwritten baselines are risky and resampling can stretch or bend strokes.

## Pagekit's observed behaviour

Not built yet. Pagekit does not detect or correct curvature.

## General technique

Model the page as a generalised cylinder: the page surface bends only in one direction, so every vertical ruling on the flat page stays straight on the curved page, and only the horizontal lines curve. Estimate two curves (for example, the top and bottom lines of writing, or the top and bottom paper edges) by tracing text lines or edges and fitting smooth splines to them; use a robust choice among candidate curves (for example random sample consensus) so a single wild line does not decide the model. From the two curves, recover the surface and unroll it, mapping each output pixel back to the source. Validate the model (the curves must form a sensible, convex quadrilateral and must not cross), and fall back to no correction with a flag when it fails. For handwriting, also allow the curves to be set or corrected by hand.
Source: H. Cao, X. Ding, C. Liu, "Rectifying the bound document image captured by the camera: a model based approach", Proc. International Conference on Document Analysis and Recognition, 2003; J. Liang, D. DeMenthon, D. Doermann, "Geometric rectification of camera-captured document images", IEEE Transactions on Pattern Analysis and Machine Intelligence, 2008; M. A. Fischler and R. C. Bolles, "Random sample consensus", Communications of the ACM, 1981; M. Kass, A. Witkin, D. Terzopoulos, "Snakes: active contour models", International Journal of Computer Vision, 1988

## Settings in general terms

The working resolution for line tracing depends on line spacing and stroke width. The smoothness of the fitted curves depends on how sharply the paper bends near the binding. How strongly curvature is translated into horizontal stretching depends on the depth of the page curve, which varies by volume and is best judged by recognition results. The validity tests depend on the page geometry. Whether automatic dewarping is used at all should be decided by measured recognition gain on curved pages, page by page or by volume.
