# Finding: a grey page can lose evidence that only colour carried

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Some captures carry real colour: a coloured stamp, a later annotation in a different ink, a red rubric, a pencil mark over brown ink. Two marks of different hue but similar brightness become the same grey, so a grey page can merge marks that a reader needs to tell apart. Keeping a colour copy only for reference does not help if the page that is actually passed on to readers is the grey one.

## Pagekit's observed behaviour

Already: the prepared page keeps the source colour, so the main output never loses colour today.

Differently: the grey tone view is made without any check of whether the colour carried meaning; it is labelled as a view for some readers, not as a page that might replace the colour one.

Not yet: any measure of how much colour a page carries; any flag when a grey candidate would merge distinct colours; any rule that a grey main page on a genuinely coloured source needs review.

## General technique

Converting colour to grey is a projection from three values to one, and some colour differences fall exactly in the direction that is lost. Colour-to-grey methods that preserve salience measure how far apart neighbouring colours are and check whether that difference survives in grey. A simpler engineering check is enough to decide review: measure the chroma (distance from the neutral axis) of the page, and where chroma is clearly above the scanner's noise in regions that hold marks, mark the grey candidate as needing a person's decision, or keep the source mode.
Source: Gooch, Olsen, Tumblin and Gooch, Color2Gray: salience-preserving color removal, ACM Transactions on Graphics (SIGGRAPH), 2005

## Settings in general terms

- The chroma level counted as real colour: should depend on the measured noise of neutral areas in the same capture, so that sensor noise does not count as colour.
- The area of coloured marks that triggers review: should depend on the size of the smallest meaningful mark, such as a short annotation or a stamp.
- Whether grey may be the main page without review: only when the colour check finds nothing, or when a recorded collection rule covers the batch.
