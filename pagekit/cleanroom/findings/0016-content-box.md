# Finding: choosing the content region, including marginalia, signatures and marks

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

The content region is what the crop must keep. On parish registers this is not just the main text block: it includes marginal names and notes, signatures, the crosses that stand for a mark, paraphs, and corrections. The document records that its reference tool, tuned for printed books, keeps only regions that pass a text-line test and trims anything near the edges that lies closer to border debris than to the main text, which can discard marginalia as garbage. The lead's document asks for a looser rule: keep any ink above speck size inside the page, and exclude only shadows, scanner bed, rulers and colour targets, and foreign objects.

## Pagekit's observed behaviour

Pagekit does not choose content; that is not built yet. Its crop check is the verification side: ink inside the page but outside every crop is reported as discarded, with its share and bounding box, and a margin note left outside the crop is flagged. Its speck filter also removes strokes about one pixel thick, so a very thin mark may not count.

## General technique

Build the content region from ink, not from text lines. Inside the page box, remove border-connected dark material (shadows, backdrop, book edge) by morphological reconstruction from large dark seeds touching the border. Remove specks below a size threshold. Take the union of all remaining connected components as content, then remove only components classified as foreign: scanning targets, rulers, slips and fingers. Isolated components near the page edge are kept unless they are clearly debris (for example, they touch the paper edge, or their shape matches a tear or tape). The content box is the bounding box of what remains. Text-line detection is still useful, but as evidence for review, not as a filter.
Source: L. Vincent, "Morphological grayscale reconstruction in image analysis: applications and efficient algorithms", IEEE Transactions on Image Processing, 1993; general knowledge

## Settings in general terms

The speck size must be below the smallest meaningful mark (an i-dot, an abbreviation stroke, a punctuation point), so it depends on the writing size and the resolution, measured on the collection. The size of the seeds that identify border shadows depends on the page size. The distance from the page edge within which a component is treated as possible debris depends on how the volumes were trimmed and bound.
