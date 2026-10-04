# Spec 0003: orientation, page count and the split

Written by the host before any code, from finding reports 0001 to 0008, 0013, 0021 and
0033, spec 0001, and the papers they cite. Nothing in it comes from ScanTailor,
ScanTailor Advanced or any other GPL program.

## Purpose

Two detectors that run on a source image before anything else:

1. **Orientation**: how many quarter turns clockwise make the page upright.
2. **Page count and split**: whether the upright frame holds one page or two, and for
   two, where to cut.

Each returns an answer in the shape spec 0002 defines: a value, a confidence from 0 to
1, a short plain-language evidence sentence, and a list of flags. Each works only on
reduced working copies of the image. Neither writes any file. When a detector is not
sure, it says so with a flag; it never guesses silently. A wrong half turn or a wrong
cut ruins the page for every reader downstream, so review is always preferred over a
silent wrong answer.

## Orientation

**Value:** 0, 1, 2 or 3 quarter turns clockwise.

1. **Lines across or down.** Lines of writing make the ink profile across rows sharply
   peaked (bands of ink alternating with gaps), while the profile across columns is
   flatter. Compare how sharply the two profiles vary on a binarised, reduced copy.
   The direction with the sharper, more periodic profile is the direction the lines
   run. Connected components of running writing are usually wider than tall, which is
   a second cue. Ruled lines and the dark page edge are masked before scoring, since
   long straight marks pull the profile their way. (Baird 1987; finding 0001.)
2. **Upright or upside down.** Once the lines run across, upright and upside down
   differ in the writing's asymmetries. In each line, measure the core band and the ink
   above and below it: Latin-script hands have more mass above the core band than
   below. Line starts are aligned on the left, so the ragged edge of the text block is
   usually on the right. Each cue votes with a strength; combine them into a
   confidence. (Caprari 2000; finding 0002.)
3. **Confidence.** If the two directions in step 1 score too close to call, or the
   up-down vote in step 2 is weak, the answer is 0 turns with a flag saying orientation
   is uncertain. The flag names which part was uncertain. A page with too little ink
   (blank or nearly blank, finding 0021) gets 0 turns, confidence 0 and a flag saying
   so.
4. **Light writing on a dark ground** (a negative frame, finding 0013): if the dark
   class is most of the page and the light class forms thin strokes, the detector
   flags the page as possibly negative and does not decide orientation from it.

## Page count and split

**Value:** the page count, 1 or 2, and for 2 the cut as a straight line through two
points in the upright frame's pixel grid. A leaning cut is allowed. Also the method
that decided: fold, gap, neighbour edge or none.

The detector runs on the frame after the orientation answer is applied.

1. **Evidence, not proportions.** The frame's proportions (using each axis's
   resolution) are only a prior. The page count is decided from evidence, and the
   evidence used is recorded (finding 0003):
   - **a fold or gutter line** near the middle: a thin dark line much taller than wide,
     or a soft dark valley in column brightness, running roughly top to bottom;
   - **a wide gap in the content** near the middle, between two masses of writing that
     each have their own left and right edges.
   When the cues disagree, or none is strong, the frame goes to review with a flag,
   and the value is 1 page.
2. **Fold detector.** Two complementary parts (finding 0004):
   - a thin-line part: enhance thin dark structures much taller than wide (a
     morphological black top-hat with an element wider than the line), then search
     near-vertical lines through the enhanced image (a Hough-style search restricted to
     a small lean range). Ignore candidates in a band near the outer edges, which are
     page or book edges. Prefer the strong candidate nearest the middle. A true fold
     runs most of the page height, so the strength is relative to the page height.
   - a valley part for soft shadows: smooth the column brightness profile and look for
     a broad minimum near the middle.
   Record which part decided and its strength. If the two disagree, flag.
   (Duda and Hart 1972; Serra 1982.)
3. **Gap detector.** Binarise, remove specks and dark border bands, and reduce the
   writing to the boxes of its connected components, so gaps between letters do not
   count. Project the boxes onto the horizontal axis to get runs of columns with and
   without content. Candidate cuts are the gaps between runs. Prefer a gap that divides
   the content into two comparable halves, and among comparable gaps the widest. Tiny
   runs at the outer edges are ignored before scoring. If no gap balances the halves,
   flag rather than fall back silently. (Breuel 2002; finding 0005.)
4. **Lean.** The cut is a line, not a column. A line found by the near-vertical search
   leans already. A cut found from a gap is fitted as a line through the gap's middle
   over the height of the content. (Finding 0006.)
5. **A strip of the neighbouring page** (finding 0007). On a frame judged to hold one
   page, check for a fold or page-edge line close to one side, and for writing cut off
   by the frame edge on one side only. Either means a neighbour strip. This detector
   reports it as a flag naming the side and the line; it does not cut it off (cropping
   it away is the page box's job). If writing touches both sides, flag only.
6. **Writing across the fold** (finding 0008). After a cut is chosen, find the ink
   components that straddle it. For each, record which side holds most of it. Report
   the count and the widest overhang in the evidence, and flag if any component
   overhangs the cut by more than the overlap setting of spec 0002, since that ink
   would be lost from one page.

## Settings

Every number is a starting guess with status UNMEASURED, reported as unmeasured, kept
in a thresholds file owned by this slice: the working resolution for each detector
(enough to keep line gaps and the fold several pixels wide), the margin by which one
direction must beat the other, the up-down vote strength needed, the lean range, the
edge band in which fold candidates are ignored, the minimum fold strength relative to
the page height, the smallest gap that counts, the balance between halves, the
neighbour-strip width, and the speck size (below the smallest meaningful mark, such as
an i-dot or an abbreviation stroke).

## Behaviour the tests must pin

Tests use synthetic pages built in the test: lines of word-like ink shapes with
ascenders and descenders, left-aligned with ragged right ends, some ruled lines,
margin notes, and dark backdrops. No real register material.

- An upright page gives 0 turns. The same page turned a quarter, a half and three
  quarters gives 3, 2 and 1 turns respectively, with confidence above the setting.
- A page with ruled lines running the other way still gives the right turn.
- A page whose two directions are indistinguishable (a grid of dots, for example) gives
  0 turns and an uncertain flag.
- A blank page gives 0 turns, confidence 0 and a too-little-ink flag.
- A light-on-dark page is flagged as possibly negative.
- A spread with a thin fold line gives 2 pages and a cut on the line. A spread with a
  soft shadow valley gives 2 pages and a cut in the valley. A spread with no fold but a
  clear gap gives 2 pages and a cut in the gap.
- A leaning fold gives a leaning cut within a small tolerance of the drawn line.
- A single page with a wide pale backdrop gives 1 page and no flag. A single page with
  a dark book edge near one side gives 1 page, not a cut at the book edge, and a
  neighbour-strip flag when writing is cut off at that side.
- A spread whose cues disagree gives 1 page and a flag.
- A marginal note crossing the fold is counted in the evidence, and flagged when it
  overhangs the cut by more than the overlap.
- The same input gives the same answer every time.

## Not in this slice

Applying any turn or cut to an image (spec 0002 does that); skew; page and content
boxes; recognition engines or models as cues; volume-wide statistics.
