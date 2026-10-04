# Finding: density metadata and pixel aspect must stay consistent

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Captures carry density tags (dots per inch for each axis) that may be wrong, default values from a camera, or unequal between axes. A person may want to write a nominal print density on prepared pages. If the horizontal and vertical densities are changed by different ratios, the page's physical or displayed proportions change even though every sample is identical. Stretching the pixels to "repair" an odd ratio without evidence changes the evidence.

## Pagekit's observed behaviour

Already: a missing or implausible resolution is a flag, never a silent default; a person can give a resolution for sources that have none, stored with its origin, and a source's own resolution is never replaced by it; unequal axes are flagged and millimetre settings are converted per axis; the page is not resampled to equal axes; output carries the source's resolution or the shrunk value.

Differently: the tag is used as if it were physical scale for millimetre settings; the reconciled documents treat source tags as reported metadata unless physical scale is independently established, and make pixel dimensions and scale the main resolution controls.

Not yet: a rule that any nominal density change keeps the accepted horizontal-to-vertical density ratio; treating a ratio change as an explicit interpretation revision needing review; refusing to copy inconsistent source tags into normalized output.

## General technique

Pixel aspect ratio is the ratio of a pixel's physical width to its height, given here by the ratio of the two densities. Display and print systems use it to size the image; non-square pixels are a well-known source of distortion when ignored or altered. Keeping the ratio fixed while changing a nominal density changes only the stated print size, not the shape. Physical resolution of the manuscript is unknown unless it is measured (for example from a scale bar or a known page size) with provenance.
Source: Poynton, Digital Video and HDTV: Algorithms and Interfaces, Morgan Kaufmann, 2003

## Settings in general terms

- The density written: either the source's reported value, labelled as reported, or a nominal value the person sets, always with the same axis ratio as accepted.
- A ratio change: never a preset; a separate reviewed decision with its evidence.
- The plausible range for reported densities: should depend on the capture equipment of the collection.
