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

## Installing and running (macOS and Linux)

pagekit needs Python 3.12 or later and Pillow, nothing else. In a terminal:

```sh
python3 --version                       # 3.12 or later
python3 -m venv ~/pagekit-env           # once: a private Python for pagekit
~/pagekit-env/bin/python -m pip install Pillow
```

Then run it from the folder that holds the `pagekit` folder (in this repository, its
top folder):

```sh
cd /path/to/the/folder/that/holds/pagekit
~/pagekit-env/bin/python -m pagekit prepare ~/scans/ --output ~/prepared/
```

Inside this repository, its own environment works the same way:
`.venv/bin/python -m pagekit ...` from the repository's top folder. "No module named
pagekit" means the command was run from another folder, or with `PYTHONSAFEPATH` set
(which leaves the current folder off Python's import path). Either `cd` to the folder
that holds `pagekit` (without `PYTHONSAFEPATH`), or name it for the one command, which
works from anywhere:
`PYTHONPATH=/path/to/the/folder/that/holds/pagekit ~/pagekit-env/bin/python -m pagekit ...`.
The correction command on the review sheet is written this way.

## Preparing pages

```sh
python -m pagekit prepare scans/ --output prepared/
```

Then open `prepared/review.html` in any browser, on a laptop or a phone, to see every
page and every decision.

`prepare` turns each source image into one clean image per page. For each scan, in
order, it finds:

1. **orientation**: how many quarter turns make the page upright;
2. **pages and cut**: one page or a two-page spread, and where to cut it (the cut may
   lean);
3. for each page, **skew**: the small angle that levels its lines;
4. for each page, the **page box** (where the paper is) and then the **content box**
   (everything to keep, notes and signatures included, or none for a blank page);
5. the **margin** around the content, then one resampling of the original into the
   prepared page.

A blank page is still written, as an image of its paper, so the sequence of pages stays
complete. Sources are a folder (its `.png`, `.tif`, `.tiff`, `.jpg` and `.jpeg` files)
or a list of files; they are only read. Nothing is ever written inside a source folder.

Options: `--project FILE` and `--overrides FILE` (below), `--report-stale`,
`--format tiff|png`, `--max-dpi N` to shrink pages above that resolution, `--dpi N`
for sources that carry no resolution (see Resolution), and
`--tone-view` (below). Exit status is 0 when no page is flagged, 1 when any page needs
review or any source file was skipped, and 2 when the command cannot run at all (and
then nothing is written).

A source file that cannot be used (unreadable, cut short, not an image, or in a mode
or depth pagekit does not read, such as 16-bit grey or CMYK) is skipped, not fatal:
the other scans are prepared as usual, and the skipped file is named with a plain
reason in the command's output, at the top of the review sheet and in the manifest's
`skipped`. It gets no page and no new project entry, so the next run tries it again
(and a file the project already knew keeps its values meanwhile). Exit status 2 is kept
for what stops the whole run: no usable source at all (every bad file is named in one
message), an output folder inside a source folder, an output folder that cannot be
written, an unreadable project or overrides file, or an invalid option.

When pagekit is not sure of a step, it says so with a flag and the page goes to review;
it never guesses silently. If a step fails on one page, that page gets a flag naming the
step and the error, the step takes a neutral default (no turn, one page, no skew, the
whole page), and the rest of the batch carries on. The margin is a setting, not a
detection: unless set by hand it is `margin_mm`, with no flag.

A full-size scan (about 3000 by 4500 pixels) takes a few seconds. Every detector works
on a reduced copy: the page and content boxes are measured on a copy of the levelled
page at `detector_working_dpi`, made from the original in one resampling, and their
boxes are scaled back outward. The prepared page itself is always made from the
original.

### The orientation tag

Some image files carry a tag saying how their stored pixels must be turned, or turned
and mirrored, to show the picture upright: the orientation tag of the Exif standard
(CIPA DC-008, Exif 2.32, 2019), which defines eight values. pagekit reads it and applies
its transform once, exactly (no resampling), before anything else: orientation, the
split and every later step see the corrected frame, and a person's quarter turns come
after it. The transform is the first link of the geometry chain (an `orientation_tag`
step, only when a tag is applied), so the point maps still lead back to the stored
pixels. Prepared pages carry no orientation tag, so no reader turns them again.

A tag outside the eight values is flagged and the source taken as stored. Whether to
trust tags is the `trust_orientation_tag` setting (default 1, trust); for one source,
an overrides line `{"source": ..., "step": "tag_trust", "value": false}` ignores its
tag, the orientation evidence says so, and later runs keep it. The manifest's
`orientation_tag` and the project's record name the value found, whether it is trusted
and applied, and the transform.

### A grey main page

`--output-mode grey` makes the pages of the sources in that run grey (8-bit, still
lossless); `--output-mode source` (the default) keeps the source's colour mode. The
choice is recorded with those sources in the project and kept for them on later runs;
sources added later are not covered. For one page, an overrides line
`{"source": ..., "step": "output_mode", "page": 1, "value": "grey"}` (or `"source"`,
optionally with `"lock": true`) sets it by hand.

The grey page goes through exactly the same chain as the colour page and is then
converted pixel by pixel: no flattening, no tone curve, no sharpening (the tone view
stays separate). When every pixel of the source has equal channels, the common channel
is kept, so every intensity is unchanged, and the manifest says the conversion was
exact. Otherwise the `grey_rule` setting (or `--grey-rule`) decides: `luminance`
(the ITU-R BT.601 weights, 0.299 red + 0.587 green + 0.114 blue), or one channel,
`red`, `green` or `blue`; the conversion is then a reviewed one.

Before a colour page is made grey, pagekit measures its colour: the chroma of each
pixel (largest minus smallest channel) on a reduced copy, against the chroma noise of
the page's own plain paper. If marks stand clearly above that noise
(`colour_chroma_margin`) over at least `colour_min_area_mm2`, the page is flagged ("this
page holds colour that grey would remove", naming where) and kept in colour, unless grey
was set by hand or locked for it, in which case it is grey and the flag stays. Colour
that is only sensor noise does not count. The manifest's `output_mode` records the mode
written, the choice and who made it, the rule, whether it was exact, and the colour
measure; the review sheet shows the same with lines to keep the page as scanned or to
force grey.

### Padding apart from the margin

The margin keeps photographed paper around the content box (clamped to the page box
plus the allowance), as before. Padding is separate, default none: a band of the
page's measured paper colour added around the finished page, `--padding 4mm`
(converted per axis with the resolution; a source with no resolution gets none, with a
flag) or `--padding 20px` (settings `padding_mm` and `padding_px`, one or the other). It
enlarges the canvas without changing the content's scale or position relative to the
source: it is the last link of the geometry chain (a `pad` step, only when set), so the
point maps include it.

The manifest's geometry also records, for every page, `margin_box` (in the levelled
grid and as its four corners in the stored source's pixels) and `regions`: the canvas,
the padding on each side, the content area, the `photographed` polygon (the part of the
page that comes from the source) and what is fill (everything else, in the paper
colour).

### Defaults

- Pages are written as lossless TIFF (deflate). PNG is available with `--format png`;
  no lossy format is offered.
- A colour scan gives a colour page and a grey scan a grey page. pagekit never turns a
  page into black and white.
- Pages keep the source resolution. They are shrunk only when you ask (`--max-dpi`).

### The review sheet

`review.html` sits beside the manifest. It is one file with everything inside it: no
internet, no scripts and no fonts from elsewhere. A short table at the top lists every
scan, flagged ones first, the most flagged first, each linking to its section, in the
same order. For each scan it shows a small preview of the original
turned upright, with the cut (vermilion), each page box (blue) and each content box
(green) drawn on it, and a small preview of each prepared page. For each step it gives
the value, where it came from, the confidence, the evidence and every flag in plain
words, and under it the exact line to copy into an overrides file to change it. The
previews are small JPEG copies (the original at most `preview_long_side_px`, 320
pixels, each page at half that), only for looking, and the browser loads them only as
you scroll to them, so a sheet of hundreds of scans stays light. A note at the top says that the
settings are not yet measured: until they are, a flag means "look at this page" and no
flag is not proof that the page is right.

To correct a page from the sheet: copy the line under the step, change the value, put
it in `overrides.json` in the output folder, and paste the command the sheet prints
into a terminal. It is one line, written with full paths, the Python that made the
sheet and `PYTHONPATH=` set to the folder that holds pagekit, so it works from any
folder (also where `PYTHONSAFEPATH` is set), and only what depends on the change is
redone. It is for macOS and Linux shells; a Windows form is not written yet.

### Pages unlike the rest of the batch

After every page has its values, pagekit compares each page's skew, content width and
height, and four margins with the rest of the batch: the median and the median absolute
deviation, with a floor on the spread. A page far from the middle is flagged, naming the
measurement and how far off it is. This needs at least `volume_min_pages` pages with
that measurement; a smaller batch is not compared. Blank pages are left out.

### Values set by hand and the detectors

A value set by hand is never detected again. Its detector still runs to compare, and if
it is confident (no flags) and far from the hand-set value (the `compare_*` settings),
the manifest and the review sheet say so in the evidence. That is a note, never a
change and never a flag. The project file keeps the person's own evidence. A run that fails at any point, even while writing, leaves the output folder and
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

A detected value's inputs hash also covers the sha256 of every pagekit file its
detector reads: its settings files (for the content box, the crop check's
`thresholds.toml` too) and its own code, down to the shared helpers. Changing a
threshold or a detector recomputes every value that detector decided, and nothing else.

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
included, and `content_box` `null` for a blank page (a box partly or wholly outside the
levelled page is flagged, since only filled-in paper colour lies there); `margin` millimetres; `resolution`
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
is a flag, never a silent default: those settings are then applied as 0 px, and the
flag says how to give it. `--dpi 300` gives every source that carries no resolution
300 dots per inch; it is stored in the project with origin `override`, so later runs
keep it, and a source's own resolution is never replaced by it. A value outside the
plausible range is refused. A resolution line in the overrides file sets one source. Unequal
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

Pages are written losslessly, TIFF with deflate compression (the default) or PNG
(`output_format`), as `<source stem>_p<page>.tif` (or `.png`); greyscale stays
greyscale and colour stays colour, and the file carries its resolution (the source's,
or the shrunk value; none when the source has none). TIFF files are written by
pagekit's own writer (`pagekit/_tiff.py`, shared with the tone view), so the same
pixels always give the same bytes.

### The manifest

`OUTPUT/pagekit-prepare.json`, schema `pagekit-prepare.v1`, closed: for each page the
source's name and sha256; the output's name, sha256, byte size, the sha256 of its
decoded pixels (`pixels_sha256`, which holds whatever the file format), format, mode,
pixel size
and resolution; the source resolution and its origin; the geometry chain as plain
parameters, with both composed affine maps (source to output and back) and the fill
colour; every step's value with origin, confidence, evidence and flags; the flags; and
the verdict, `review` or `no_flags`. `batch` holds the volume-wide comparison: for each
measurement the number of pages, whether it was compared, the median, the spread and how
many pages were flagged. `review` names the review sheet. `skipped` lists the source
files that could not be used, each with `name`, `path` (from the project file's folder),
`sha256` (null when the file cannot be read) and `reason`; they have no pages.
`stale_outputs` lists the output files of pages
that no longer exist, which are left in place, never deleted. Like the crop check it reports `thresholds`,
`thresholds_measured` and `thresholds_note`. The same project gives byte-identical
images and manifest.

Coordinates are continuous: pixel (i, j) covers [i, i+1) x [j, j+1). A point on a
prepared page maps back to the source through `affine_output_to_source`
`[a, b, c, d, e, f]` as (a x + b y + c, d x + e y + f), and `pagekit.geometry.Chain`
maps points and polygons both ways.

### The grey tone view

`--tone-view` also writes the grey tone view of spec 0006 (`pagekit/tone.py`) beside
each page, as `<page>_tone.tif`, made by tone.py from the prepared page and written by
its own deterministic TIFF writer. The manifest records each view's name, sha256 and
the tone record with its settings. A view is never written over a prepared page or a
source: pagekit stops first and writes nothing. The view is for the readers that need
grey; the prepared page itself is unchanged.

### Measuring success

```sh
python -m pagekit measure --prepared prepared/ --gold answers.json [--json]
```

`measure` compares a prepared batch with a hand-checked answer file (`pagekit-gold.v1`):

```json
{"schema": "pagekit-gold.v1", "sources": [
  {"source": "0012.tif", "orientation": 1, "pages": 2,
   "cut": [[1510, 0], [1532, 4480]], "skew": [0.4, -0.2],
   "content_box": [[120, 200, 1450, 4300], null]}
]}
```

`source` is the file name or sha256; every other key is optional. The cut is in the
pixels of the upright image. Each content box is drawn on the upright image after
turning it by that page's true skew about its centre, keeping its size (as an image
editor levels a picture); `null` is a blank page. pagekit's box is mapped into that
same grid before the two are compared. For each
step it reports how many were right (within the `measure_*` tolerance and not flagged),
wrong (outside it and not flagged: the errors that matter most, listed by name) and sent
to review (flagged), with the size of the errors. It changes no setting. Exit status 0,
or 2 when a file cannot be used.

### For other code

`pagekit.answer` defines a detector's answer (value, confidence, evidence, flags) and
refuses one that breaks the shape. `pagekit.prepare.plan(..., detectors={step:
Detector(method, run, settings, compare)})` calls each detector in step order with a
`StepContext` (earlier values, settings, the original image, and
`working_copy(long_side)`, a reduced copy of the step's grid made from the original)
and stores its answer; `pagekit.pipeline.DETECTORS` are the connected detectors (with no
detectors given, every step takes its neutral default with a flag);
`pagekit.output.execute(plan)` writes the pages, the manifest, the project file and the
review sheet.

### Settings

`thresholds_prepare.toml` holds the settings of `prepare` and `measure`: the overlap,
margin and allowance, the plausible resolution range, shrinking and the output format,
the detectors' working resolution, the comparison with hand-set values, the batch
checks, the preview size and the `measure` tolerances. The detectors' own settings are
in `thresholds_split.toml` (orientation and split) and `thresholds_skew.toml` (skew and
the boxes). All are starting guesses with status `UNMEASURED`, and the review sheet
lists them.

### Not yet

- Every setting is an unmeasured guess until a hand-checked set of real pages is run
  through `measure`.
- Output files of pages that no longer exist (a spread re-split into one page) are
  left in the output folder and listed under `stale_outputs`; pagekit never deletes.
- EXIF orientation tags are ignored: sources are taken in their stored pixel grid.
- Source modes handled: greyscale, colour, bilevel (written as greyscale) and palette
  (written as greyscale when every colour it uses is a grey, else as colour). Others,
  such as 16-bit greyscale or CMYK, are skipped with a reason (above).

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
