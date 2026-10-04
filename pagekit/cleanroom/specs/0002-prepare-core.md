# Spec 0002: the preparation core (project, geometry, output)

Written by the host before any code, from pagekit's own README and spec 0001, finding
reports 0014, 0021, 0031, 0033 and 0034, and general image-processing knowledge.
Nothing in it comes from ScanTailor, ScanTailor Advanced or any other GPL program.

## Purpose

pagekit is to prepare scanned register pages the way a person prepares them by hand
before reading: put each page upright, split a two-page spread, level a tilted page, find
the paper and the writing, add a margin, and write one clean image per page. This
slice builds the part every step depends on, without any detector:

- a **project file** holding, for every source image and every page cut from it, each
  step's value, where that value came from and what it was computed from;
- a **geometry chain** that turns a source image into a prepared page with one
  resampling, and maps any point back and forth between the two;
- an **output writer** that writes the prepared pages and a manifest.

The detectors (orientation, page count and split, skew, page box, content box) are
built in other slices. This slice must work with steps whose values are given by hand
or by a test, so it can be tested on its own.

## Steps and their values

The steps, in the order they apply, and the value each holds:

1. **orientation**: a number of quarter turns clockwise, 0, 1, 2 or 3, that makes the
   source upright.
2. **split**: how many pages the upright frame holds, 1 or 2, and for 2 the cut, a
   straight line given by two points in the upright frame's pixel grid (so it may
   lean). The two pages are the left and right sides of the cut. A page keeps an
   overlap past the cut (a distance in millimetres, see Settings) so that a stroke
   crossing the fold appears whole on at least one page.
3. **skew**: for each page, the angle in degrees by which the page is turned
   counterclockwise to make its lines level. Positive means counterclockwise. Zero
   means no rotation.
4. **page box**: for each page, the box of the paper in the levelled page's grid, left,
   top, right and bottom, right and bottom not included.
5. **content box**: for each page, the box of everything to keep, in the same grid, or
   none for a blank page.
6. **margin**: the margin, in millimetres, added around the content box, clamped to
   the page box plus an allowance (see Settings). A blank page keeps its whole page
   box.

## Where a value comes from

Every step value carries:

- **origin**: detected, manual or locked. Detected values come from a detector. Manual
  values were set by a person. Locked values were set by a person and must never be
  changed by a re-run, even when flagged.
- **confidence**: a number from 0 to 1, or none for manual and locked values.
- **evidence**: a short plain-language sentence saying what decided the value.
- **flags**: a list of plain-language reasons the page should be looked at. An empty list
  means none.
- **inputs hash**: the sha256 of everything the value was computed from: the source
  file's sha256, the values of the earlier steps it depends on, and the settings the
  step read.

A detector's answer has the same shape: value, confidence, evidence and flags. This
slice defines that shape, validates it, and refuses an answer that breaks it.

## The project file

One JSON file per batch, schema name `pagekit-project.v1`, closed (unknown keys are
refused). It holds the settings used, and for each source image:

- its path relative to the project file, its sha256, byte size and pixel size;
- its resolution for each axis, and where that came from: the file, an override in
  the project, or missing. A missing or implausible resolution with no override is a
  flag, never a silent default (finding 0014). A plausible range is a setting.
- the steps above, with their values and the fields under "Where a value comes from";
- the pages it produced, each with its output file name.

The same inputs and the same step values give a byte-identical project file. Writing
the file is atomic: a crash never leaves a half-written project.

### Re-running and corrections

- Detected values are recomputed when their inputs hash changes. A detected value
  whose inputs hash is unchanged is kept as it is.
- Manual values are kept. If their inputs hash has changed (for example the split was
  moved), the value is kept and a flag says it should be checked because what it was
  set on has changed.
- Locked values are kept and never flagged for that reason.
- Before running anything, pagekit can report which pages and steps are stale and
  why, without changing any file.
- **Overrides file.** A person corrects a value by writing a small JSON file, schema
  name `pagekit-overrides.v1`, naming the source image (by its path or sha256), the
  page where the step is per page, the step and the new value, and optionally lock.
  Applying it sets those values with origin manual (or locked) and recomputes only what
  depends on them. An override naming an image, page or step that does not exist is
  refused with a plain message, and nothing is changed.

## The geometry chain

For every prepared page, the chain from the source image to the output is: the quarter
turn, the page's side of the cut (as a polygon, so a leaning cut is kept), the small
rotation about the page's centre, the crop to the margin box, and any scale. The chain
is stored as plain parameters in the manifest, in a form that does not depend on
pagekit's code (finding 0034).

- **One resampling.** The prepared image is made from the original source in a single
  resampling through the composed chain. Detectors may make their own reduced working
  copies, but no output is ever made from a working copy or from another output
  (finding 0031).
- **Kernels.** Quarter turns are exact (no resampling). A small rotation uses a
  high-quality interpolation kernel. Shrinking uses area averaging. The output is never
  larger than the source resolution allows: no upsampling.
- **Outside the paper.** Any area of the output that falls outside the source image, or
  on the neighbour's side of the cut, is filled with the paper's colour. The paper
  colour is estimated from the light class of the page (pixels above an ink threshold),
  as a robust central value such as the median, so ink and stains do not pull it
  (finding 0020). It is never a synthetic pure white or black.
- **Mapping points.** pagekit provides a forward map (source point to output point) and
  an inverse map (output point to source point) for points and polygons. A point
  mapped forward and back returns within a small fraction of a pixel at the source
  resolution.

## Output

For each prepared page, an image file and a manifest entry.

- **Image.** Lossless: TIFF with lossless compression, or PNG, chosen by a setting.
  Greyscale sources give greyscale output. Colour sources keep colour. The output
  resolution is written into the file, equal to the source resolution unless the page
  was shrunk, and then the shrunk value.
- **Manifest.** Schema name `pagekit-prepare.v1`, closed. For each page: the source
  file's name and sha256, the output file's name and sha256, its pixel size and
  resolution, the full geometry chain, every step's value with origin, confidence,
  evidence and flags, and a verdict: `review` when any flag is set, otherwise
  `no_flags`. Thresholds that are not measured are reported as such, as in spec 0001.
- A blank page is still written as an image of the paper, never dropped, so the
  sequence of pages stays complete (finding 0021).
- Outputs never overwrite a source image and never write inside the source folder.
- The same project gives byte-identical images and manifest.

## Command line

`python -m pagekit prepare` is the command a person uses. In this slice it runs with
the detectors absent: steps without a value take a neutral default (no turn, one page,
no skew, page box the whole frame, content box the whole page, the margin setting) and
each default is recorded with origin detected, confidence 0 and a flag saying the step
was not run. It accepts a folder of source images or a list of files, an output folder,
an optional project file to continue from, an optional overrides file, and
`--report-stale` to list stale steps without running. Exit status: 0 when no page is
flagged, 1 when any page needs review, 2 when the input cannot be used (nothing is
written in that case). The existing `check` command is unchanged.

## Settings

Every number is a starting guess, kept in pagekit's thresholds files with status
UNMEASURED, and reported as unmeasured, as in spec 0001: the overlap past a cut in
millimetres, the margin in millimetres, the allowance past the page box, the plausible
resolution range, and the output format.

## Interfaces the other slices use

Other slices build detectors that return the answer shape above. This slice owns the
answer shape and its validation, the project file, the geometry chain and point
mapping, the output writer and the `prepare` command. It must expose them so a later
slice can call each detector in step order, store its answer, and run the chain.

## Behaviour the tests must pin

- A synthetic page with values given by hand (a quarter turn, a split, a small angle, a
  page box and a content box) is prepared from the original in one resampling, and
  lands where those values say: a mark drawn at a known source point appears at the
  forward-mapped output point, and the inverse map takes it back within a fraction of a
  pixel.
- A leaning cut keeps the neighbour's wedge out of each page, filled with paper colour.
- No output is upsampled beyond the source resolution.
- A re-run with unchanged inputs gives byte-identical images, project file and
  manifest.
- An override is kept on re-run. A manual value whose inputs changed is kept and
  flagged. A locked value is kept and not flagged. An override naming something absent
  is refused and changes nothing.
- `--report-stale` lists stale steps and writes nothing.
- A missing resolution with no override is a flag, not a default.
- A blank page is written as an image of the paper.
- Unusable input is exit status 2 and writes nothing. A source image is never changed.

## Not in this slice

Detectors of any kind; dewarping; binarisation, despeckle and other image cleaning;
colour work; a graphical interface.
