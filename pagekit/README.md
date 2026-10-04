# pagekit

Page-preparation checks for scanned masters. The first slice checks a declared crop
against the uncropped master it was cut from, so that a crop which threw away writing
is sent to review before anything downstream sees the page. A check on the cropped
page alone cannot do this: writing cut off cleanly leaves no trace there.

pagekit only reads the master; it never writes it, and it never changes a crop. Every
flag means "send this page to review".

```sh
python -m pagekit check --master scan.tif --crop 120,90,2480,3400 [--crop ...] \
    [--split-x 2510] [--min-short-side-px 1200] --json
```

Crop boxes are `x0,y0,x1,y1` in the master's stored pixel grid (no EXIF rotation),
right and bottom exclusive. Exit status is 0 when nothing is flagged, 1 when the page
should go to review and 2 when the inputs cannot be checked. Any failure to read the
master is exit 2, including an image mode pagekit does not handle: a 16-bit greyscale
(`I;16`) master is refused loudly rather than reduced to 8 bits unseen. Without `--json` the
command prints the verdict and the reasons; with it, the full report.

## What it checks

The master is reduced to grey and split into ink and paper with Otsu's threshold
(Otsu, 1979), after border rows and columns that are nearly all dark (scanner backdrop)
are trimmed off as outside the page. Ink pixels with no ink among their eight
neighbours (lone specks) are cleared before anything is counted; a pen stroke one pixel
thick is kept, since each of its pixels touches the next. If the page shows no ink at
all (its dark and light levels too close together), the checks cannot run and the page
is sent to review with that reason; it is never passed as `no_flags`.

1. **Ink discarded.** Ink inside the page but outside every crop: pixel count, share of
   the page's ink and bounding box. A margin note or catchword left outside the crop
   shows up here.
2. **Ink cut at an edge.** For each edge of each crop, a thin band just inside and one
   just outside. Positions along the edge with ink in both bands are writing that runs
   across the edge; their count and spans are reported.
3. **Split against the gutter.** With `--split-x`, or with two crops side by side, the
   column ink profile of the page is searched near the middle for the lowest-ink run,
   taken as the gutter. The report gives the split's distance from it and the ink in
   the split column itself.
4. **Resolution.** Pixel size and DPI metadata. Missing DPI, or a short side below the
   minimum, is flagged.

## The report

`pagekit-crop-check.v1` is a closed JSON object: `schema`, `tool`, `input` (file name,
SHA-256, byte size, crops, split), `page` (page area, threshold, background, ink
contrast), `checks` (one entry per check above, each with its measurements, `flag`
and plain-language `reasons`), `flags`, `verdict` (`review` or `no_flags`),
`thresholds`, `thresholds_measured` and `thresholds_note`. The same master and crops
give byte-identical JSON wherever the file sits.

## Thresholds

The thresholds live in `thresholds.toml`. None has been calibrated on real pages yet:
each carries `status = "UNMEASURED"`, and the report says `thresholds_measured: false`
until every one is measured. Until then a flag means "look at this page", and no flag
is not proof the crop is right.

## Not yet

- Thresholds are uncalibrated (above).
- The page area is found only by trimming dark borders; a pale backdrop, a scanner
  shadow that does not fill a whole row or column, or a neighbouring page in the frame
  is counted as page, and its ink as discarded.
- The ink threshold is global. Uneven lighting, faded ink or show-through can move
  writing to the paper side, or stains to the ink side.
- Specks of two or more touching pixels count as ink, and a lone speck on the image's
  outermost row or column is kept.
- The gutter is the lowest-ink run of columns near the middle; a dark gutter shadow
  that reads as ink, a skewed spread or a page with no blank gutter can mislead it.
- Crops are axis-aligned boxes; rotation and deskew are not checked.
- No crop is proposed or corrected. Detection of page edges, content boxes, deskew and
  dewarping are later slices.

## Preparing pages

```sh
python -m pagekit prepare scans/ --output prepared/ \
    [--project prepared/pagekit-project.json] [--overrides fixes.json] [--report-stale]
```

`prepare` turns each source image into one clean image per page: a quarter turn to
upright, a split of a two-page spread along a straight cut (which may lean), a small
rotation to level the lines, a crop to the content plus a margin, and an optional
shrink. Sources are a folder (its `.png`, `.tif`, `.tiff`, `.jpg` and `.jpeg` files) or
a list of files; they are only read. Outputs, the manifest and the project file never
go inside a source folder.

The detectors (orientation, split, skew, page box, content box) are built in later
slices. Until they are connected, each of those steps takes a neutral default (no turn,
one page, no skew, the whole page), recorded with confidence 0 and a flag saying the
step was not run, so every page goes to review unless a person set its values. The
margin is a setting, not a detection: unless set by hand it is `margin_mm`, recorded as
detected with confidence 1, evidence naming the setting, and no flag. Exit status is 0
when no page is flagged, 1 when any page needs review and 2 when the input cannot be
used. A run that fails at any point, even while writing, leaves the output folder and
the project file as they were: every file is first written in full beside its target,
then all are moved into place together.

### Steps and where their values come from

Each step value carries its origin (`detected`, `manual` or `locked`), a confidence
(none for values set by hand), a sentence of evidence, flags, and an inputs hash: the
sha256 of the source's sha256, the earlier steps' values and the settings the step
read. On a re-run:

- a detected value is recomputed when its inputs hash or its detector's method changes,
  and kept otherwise;
- a manual value is kept; if what it was set on has changed, it is flagged for a check.
  Applying the same correction again does not clear the flag: it keeps the inputs it
  was first set on. After checking it, lock it (`"lock": true`) or give a new value;
  either sets it afresh on the current inputs;
- a locked value is kept and never flagged for that.

Values set by hand on a page that no longer exists (a spread set back to one page) are
never discarded. They stay in the project under the source's `dropped_pages`, the
remaining pages are flagged about them on every run, and they come back if the page
does. To discard them, delete that page's entry from `dropped_pages` in the project
file.

`--report-stale` lists the steps that would change and why, without running anything
or writing any file (exit 1 when something is stale, 0 when nothing is).

### Corrections

A person corrects a value with an overrides file and `--overrides`:

```json
{"schema": "pagekit-overrides.v1", "overrides": [
  {"source": "scans/0012.tif", "step": "split",
   "value": {"pages": 2, "cut": [[1510, 0], [1532, 4480]]}},
  {"source": "scans/0012.tif", "step": "skew", "page": 2, "value": -0.6, "lock": true},
  {"source": "scans/0013.tif", "step": "content_box", "page": 1, "value": null},
  {"source": "scans/0014.tif", "step": "resolution", "value": [400, 400]}
]}
```

`source` is a path relative to the overrides file, or the source's sha256. Pages count
from 1. The values: `orientation` 0 to 3 quarter turns clockwise; `split`
`{"pages": 1}` or `{"pages": 2, "cut": [[x, y], [x, y]]}` in the upright frame's
pixels; `skew` degrees counterclockwise, under 45; `page_box` and `content_box`
`[left, top, right, bottom]` in the levelled page's pixels, right and bottom not
included, and `content_box` `null` for a blank page; `margin` millimetres; `resolution`
`[x_dpi, y_dpi]`. An entry may add `evidence`, a sentence saying why. Values are set as
manual, or locked with `"lock": true`, and only what depends on them is recomputed. An
override naming a source, page or step that does not exist is refused (exit 2) and
nothing changes.

### The project file

`pagekit-project.v1`, written to `OUTPUT/pagekit-project.json` unless `--project` names
another, and continued from on the next run, so corrections are never lost. It is a
closed JSON object: the settings used, and for each source its path relative to the
project file, sha256, byte size, pixel size, mode, resolution and where it came from
(`file`, `override` or `missing`), the source flags, the orientation and split values,
each page with its output file name and its per-page values, and `dropped_pages`
(pages that no longer exist: their output name and hand-set values). The same inputs and
values give the same bytes, and it is written atomically. A project naming a source not
given to the run is refused; with no sources given, the project's own are used.

### Resolution

Millimetre settings (overlap, margin, allowance) need the scan resolution. A resolution
missing from the file, or outside the plausible range, with no override in the project
is a flag, never a silent default: those settings are then applied as 0 px. Unequal
axes are flagged and converted per axis; the page is not resampled to equal axes.

### Geometry and output

For each page the chain is: quarter turn, the page's polygon on its side of the cut
(kept `overlap_mm` past it), rotation about the page's centre, crop to the margin box,
and scale. The margin box is the content box grown by `margin_mm` and held to the page
box plus `margin_allowance_mm`; a blank page keeps its whole page box and is still
written, as an image of its paper. The page is made from the original source in one
resampling: an exact crop when there is no rotation, bicubic interpolation when there
is, and area averaging when shrinking. When a rotated page is also shrunk by up to 1/k,
it is sampled on a k-times finer grid and each k x k block averaged; that is one
filter applied to the original, not a second pass over a finished page. Pages are never
enlarged; `max_output_dpi` (0 keeps the source resolution) may shrink them.

Everything outside the page's polygon, including anything outside the source, is
filled with the page's paper colour: the median of each band over the pixels above
Otsu's threshold inside the page, measured on a reduced working copy.

Pages are written losslessly, PNG or TIFF with deflate compression (`output_format`),
as `<source stem>_p<page>.png` (or `.tif`); greyscale stays greyscale and colour stays
colour, and the file carries its resolution (the source's, or the shrunk value). A
3000 x 4500 colour page takes a few seconds, most of it lossless encoding.

### The manifest

`OUTPUT/pagekit-prepare.json`, schema `pagekit-prepare.v1`, closed: for each page the
source's name and sha256; the output's name, sha256, byte size, format, mode, pixel size
and resolution; the source resolution and its origin; the geometry chain as plain
parameters, with both composed affine maps (source to output and back) and the fill
colour; every step's value with origin, confidence, evidence and flags; the flags; and
the verdict, `review` or `no_flags`. `stale_outputs` lists the output files of pages
that no longer exist, which are left in place, never deleted. Like the crop check it reports `thresholds`,
`thresholds_measured` and `thresholds_note`. The same project gives byte-identical
images and manifest.

Coordinates are continuous: pixel (i, j) covers [i, i+1) x [j, j+1). A point on a
prepared page maps back to the source through `affine_output_to_source`
`[a, b, c, d, e, f]` as (a x + b y + c, d x + e y + f), and `pagekit.geometry.Chain`
maps points and polygons both ways.

### For the detector slices

`pagekit.answer` defines a detector's answer (value, confidence, evidence, flags) and
refuses one that breaks the shape. `pagekit.prepare.plan(..., detectors={step:
Detector(method, run, settings)})` calls each detector in step order with a
`StepContext` (earlier values, settings, the original image, and
`working_copy(long_side)`, a reduced copy of the step's grid made from the original)
and stores its answer; `pagekit.output.execute(plan)` writes the pages.

### Settings

`thresholds_prepare.toml`: `overlap_mm`, `margin_mm`, `margin_allowance_mm`,
`min_plausible_dpi`, `max_plausible_dpi`, `max_output_dpi`,
`paper_estimate_long_side_px` and `output_format`. All are starting guesses with status
`UNMEASURED`.

### Not yet

- No detectors: every detected step's default is flagged until a person sets it or a
  detector slice is connected.
- Output files of pages that no longer exist (a spread re-split into one page) are
  left in the output folder and listed under `stale_outputs`; pagekit never deletes.
- EXIF orientation tags are ignored: sources are taken in their stored pixel grid.
- Source modes handled: greyscale, colour, bilevel (written as greyscale) and palette
  (written as colour). Others, such as 16-bit greyscale, are refused.

## Airlock

pagekit is Apache-2.0. ScanTailor and ScanTailor Advanced are GPL-3.0, and pagekit
keeps a wall between their code and ours:

- Build-side agents never read ScanTailor or ScanTailor Advanced source code, or any
  other GPL page-processing code, and never reproduce it.
- Later, a separate reader agent may compare ScanTailor's behaviour with pagekit's and
  report only plain-language findings: the situation, the general technique and the
  settings that matter. Never code, and never "copy this".
- The build side works from those reports only, together with published methods and
  general image-processing knowledge.
- The credit stays in `NOTICE`.

The full protocol, the checks that enforce it and the record are in
[cleanroom/CLEANROOM.md](cleanroom/CLEANROOM.md).

## Status

pagekit lives in this repository as a top-level folder that imports nothing from
the rest of it, so it can become its own repository at beta. `pyproject.toml` here is
for that split; the parent project does not read it. Its only dependency is Pillow.
