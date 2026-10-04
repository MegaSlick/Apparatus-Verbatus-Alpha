# Spec 0004: skew, the page box and the content box

Written by the host before any code, from finding reports 0009 to 0012, 0015, 0016,
0018 to 0021 and 0023, spec 0001, and the papers they cite. Nothing in it comes from
ScanTailor, ScanTailor Advanced or any other GPL program.

## Purpose

Three detectors that run on one page after orientation and the split:

1. **Skew**: the small angle that levels the page's lines.
2. **Page box**: where the paper is inside the frame.
3. **Content box**: what must be kept, marginalia and signatures included, and whether
   the page is blank.

Each returns an answer in the shape spec 0002 defines: a value, a confidence from 0 to
1, a short plain-language evidence sentence, and a list of flags. Each works only on
reduced working copies. None writes any file. When unsure, a detector flags; it never
guesses silently.

The input to each is one page: an upright greyscale image with its resolution for each
axis, and optionally a polygon marking the page's own area (the page's side of a cut),
outside which nothing counts.

## Skew

**Value:** degrees counterclockwise that level the lines. Positive is counterclockwise.

1. **Measure only on writing** (finding 0010). Work inside the page's area. Remove very
   large dark blobs and long rules first: a morphological opening with an element much
   longer than any word keeps only wide bands and long rules; reconstruction from those
   survivors recovers each whole band, which is subtracted from the ink. Long, thin,
   nearly straight components are removed by their shape. (Vincent 1993.)
2. **Projection-profile estimate** (finding 0009). For each candidate angle, project the
   binarised writing onto rows after rotating or shearing by that angle. When the angle
   matches the lines, the profile alternates sharply between line bands and gaps. Score
   the sharpness (for example the energy of differences between neighbouring rows).
   Search coarsely over the range on a reduced copy, then refine near the best angle.
   (Postl 1986; Baird 1987.)
3. **A second, independent estimate** as a cross-check: for example the median angle of
   chains of neighbouring components along each line, or a near-horizontal line search
   on a map where each line of writing is smeared into a band. When the two disagree by
   more than a tolerance, flag. (Le, Thoma and Wechsler 1994.)
4. **Trust** (finding 0012). Trust the result only when the best score stands clearly
   above the typical score across the range. Otherwise the value is 0 and a flag says
   why. Specific outcomes, each with its own reason:
   - too little writing for a profile: 0, confidence 0, "too little content";
   - best angle at the edge of the search range: widen once; if still at the edge, flag;
   - local estimates (per region of the page) that disagree: apply the dominant angle
     only if it covers most of the writing, and flag either way.
5. Very small angles are snapped to 0 to avoid a needless resample, and the evidence
   says so.

## Page box

**Value:** left, top, right and bottom of the paper in the page's grid.

1. Find the paper (finding 0015). Binarise the frame (try more than one threshold
   method, since backdrops vary). Walk inward from each side while the scanline is
   almost entirely backdrop. The paper edge is where the run of backdrop lines ends and
   stays ended for a set distance, so dust, tears and labels at the edge do not stop the
   walk. For a pale backdrop, use the change in brightness between backdrop and paper
   instead of darkness.
2. A scanner shadow that does not fill a whole row or column, or a neighbour strip
   reported by the split detector, must not be counted as page. The box excludes it
   where it can be found, and flags it where it cannot.
3. If no edge is found on a side, the box keeps the frame edge on that side, with
   evidence saying so. That alone is not a flag (many scans are cropped to the paper).
4. (Shafait, van Beusekom, Keysers and Breuel 2008; Fan, Wang and Lay 2002.)

## Content box and blank pages

**Value:** left, top, right and bottom of everything to keep, in the page's grid, or
none for a blank page. Also: whether the page is blank.

1. **Blank first** (finding 0021). Decide blankness from contrast and ink amount. A
   page with only a few marks is not blank: its marks must be kept. A blank page's
   content box is none, with evidence saying why; it is not a flag by itself, but a
   page judged blank that has any ink above speck size is flagged.
2. **From ink, not from text lines** (finding 0016). Inside the page box: remove
   border-connected dark material (shadows, backdrop, book edge) by reconstruction from
   large dark seeds touching the border; remove specks below the speck size (smaller
   than an i-dot or an abbreviation stroke); take the union of all remaining connected
   components. Isolated components near the page edge (marginal names and notes,
   signatures, crosses for a mark) are kept unless clearly debris: touching the paper
   edge, or shaped like a tear or a strip of tape. Text-line detection may be used as
   evidence, never as a filter. (Vincent 1993.)
3. **Ruled lines** (finding 0018) are kept inside the content if they are inside the
   page, but a rule alone does not extend the content box past the writing by more than
   the margin setting.
4. **Scanning targets** (finding 0019): a region of saturated, uniform colour patches in
   a grid, or a straight bar with evenly spaced ticks, is excluded from the content and
   reported in the evidence with its box. On a greyscale scan, colour cues are not
   available; the detector then relies on shape and flags a suspected target rather than
   excluding it.
5. **Uneven light** (finding 0023): before binarising for the content box, flatten a
   slow brightness gradient (for example by dividing by a heavily smoothed background
   estimate), so a gutter shadow or staining does not read as ink.
6. **Check against the crop check.** After the content box is chosen, the answer
   reports how much ink above speck size lies inside the page box but outside the
   content box, as spec 0001 measures discarded ink, and flags when it passes the
   discarded-ink thresholds of spec 0001.

## Settings

Every number is a starting guess with status UNMEASURED, reported as unmeasured, kept in
a thresholds file owned by this slice: the skew search range and refinement step, the
working resolution (enough to keep line gaps), the score margin for trust, the
disagreement tolerance between the two estimates, the snap-to-zero angle, the minimum
ink for a skew estimate, the backdrop share of a scanline, the run length that ends the
walk inward, the speck size, the border-seed size, the debris distance from the paper
edge, the blank contrast and ink amounts, and the background smoothing size.

## Behaviour the tests must pin

Tests use synthetic pages built in the test: lines of word-like ink shapes with
ascenders and descenders, rotated by known angles, with ruled lines, margin notes,
signatures, a small cross, dark and pale backdrops, a gutter shadow and a target-like
grid. No real register material.

- A page rotated by a known small angle gives that angle within a tolerance, for
  positive and negative angles.
- A ruled page whose rules are level but whose writing slopes gives the writing's
  angle, not the rules'.
- A page with a wide dark band at one side gives the writing's angle, not the band's.
- A page with too little writing gives 0 and a "too little content" flag.
- Two regions sloping differently give a flag.
- A tiny angle is snapped to 0, and the evidence says so.
- A page on a dark backdrop gives the paper's box. On a pale backdrop, the same. A page
  cropped to the paper gives the frame as the box with no flag.
- A marginal note, a signature and a small cross near the edge are inside the content
  box. A speck smaller than the speck size is not counted. A dark border shadow is not
  content.
- A blank page gives none and blank. A page with one small cross is not blank.
- A target-like grid is excluded from the content and reported.
- A gutter shadow does not pull the content box into the shadow.
- The same input gives the same answer every time.

## Not in this slice

Applying any rotation or crop (spec 0002 does that); the margin (spec 0002 applies it);
dewarping; volume-wide outlier statistics (finding 0011, a later slice); colour
calibration.
