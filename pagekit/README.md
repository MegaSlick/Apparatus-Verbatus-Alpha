# pagekit

Page preparation for scanned register masters. `prepare` turns each scan into one clean,
lossless image per page and says, page by page, what it decided and how sure it is.
`check` tests a declared crop against the uncropped master so that a crop which threw
away writing is sent to review. `measure` scores a prepared batch against hand-checked
answers.

pagekit only reads sources; it never writes them or changes their modification time. It
never guesses silently: when it is not sure of a step, it flags the page for review.

pagekit is a top-level folder that imports nothing from the rest of this repository, so it
can become its own repository; its `pyproject.toml` is for that split. Its only dependency
is Pillow.

## Installing and running (macOS and Linux)

pagekit needs Python 3.12 or later and Pillow:

```sh
python3 -m venv ~/pagekit-env           # once: a private Python for pagekit
~/pagekit-env/bin/python -m pip install Pillow
cd /path/to/the/folder/that/holds/pagekit
~/pagekit-env/bin/python -m pagekit prepare ~/scans/ --output ~/prepared/
```

Inside this repository, `.venv/bin/python -m pagekit ...` from the top folder works the same
way. "No module named pagekit" means the command was run from another folder or with
`PYTHONSAFEPATH` set; either `cd` to the folder that holds `pagekit`, or prefix the command
with `PYTHONPATH=/path/to/the/folder/that/holds/pagekit`, which works from anywhere.

## Preparing pages

```sh
python -m pagekit prepare scans/ --output prepared/
```

Then open `prepared/review.html` in any browser to see every page and every decision.

For each scan, in order, `prepare` finds:

1. **orientation**: how many quarter turns make the page upright;
2. **pages and cut**: one page or a two-page spread, and where to cut it (the cut may lean);
3. for each page, **skew**: the small angle that levels its lines;
4. only when cropping is on: the **page box** (where the paper is), the **content box**
   (everything to keep, notes and signatures included, or none for a blank page), and the
   **margin** around the content;
5. one resampling of the original into the prepared page.

**Cropping is off by default.** Each page is then its whole side of the cut (with the
overlap), upright and levelled, with nothing of the source cut away. `--crop page` crops to
the page box; `--crop content` to the content box plus the margin; an overrides line
`{"source": ..., "step": "crop", "page": 1, "value": "content"}` (or `"page"`, `"none"`)
sets one page, and a box set by hand turns cropping on for that page. With cropping off
the box detectors do not run, unless `crop_detectors_when_off` is 1 (they then report what
they would have cut, without changing the page). The manifest's `applied` and the review
sheet say what was applied to each page.

Sources are a folder (`.png`, `.tif`, `.tiff`, `.jpg`, `.jpeg`) or a list of files. Nothing
is written inside a source folder. A blank page is still written, as an image of its paper,
so the page sequence stays complete.

**Options:** `--project FILE`, `--overrides FILE`, `--report-stale`, `--format tiff|png`,
`--max-dpi N` (shrink pages above that resolution), `--dpi N` (for sources with no
resolution), `--crop none|page|content`, `--output-mode source|grey`, `--grey-rule`,
`--padding 4mm|20px`, `--tone-view`, `--cache DIR`, `--no-cache`, `--cache-full`.

**Exit status:** 0 when no page is flagged and no source is skipped; 1 when any page needs
review or any source is skipped while at least one is usable; 2 when the command cannot run
at all (no usable source, an output folder inside a
source folder or not writable, an unreadable project or overrides file, an invalid option),
and then nothing is written.

**Skipped sources.** A file that cannot be used (unreadable, truncated, not an image, or a
mode such as 16-bit grey or CMYK) is skipped, not fatal. It is named with a reason in the
output, at the top of the review sheet and in the manifest's `skipped`, gets no page and no
new project entry, and is tried again next run.

**Failures.** If a step fails on one page, the page gets a flag naming the step and the
error, the step takes a neutral default (no turn, one page, no skew, the whole page), and
the batch carries on. A run that fails at any point, even while writing, leaves the output
folder and project file as they were: every file is written in full beside its target, then
all are moved into place together.

**Speed.** A full-size scan (about 3000 by 4500 pixels) takes a few seconds. Detectors work
on reduced copies (`detector_working_dpi`); the prepared page is always made from the
original.

### Defaults

- Pages are lossless TIFF (deflate), or PNG with `--format png`; no lossy format is offered.
- A colour scan gives a colour page and a grey scan a grey page; pagekit never makes a page
  black and white.
- Pages keep the source resolution unless you ask (`--max-dpi`). Pages are never enlarged.

### The orientation tag

Some files carry an Exif orientation tag (CIPA DC-008, Exif 2.32) saying how the stored
pixels must be turned or mirrored to show the picture upright. pagekit applies a trusted tag
exactly once, with no resampling, before anything else; a person's quarter turns come after
it. Prepared pages carry no orientation tag.

pagekit works on each source as Pillow opens the file's bytes, and the manifest's
`source_size` and point maps refer to that grid:

- **PNG and JPEG** (Pillow leaves them as stored): the grid is the stored pixels; the chain
  applies the tag as its first link (`orientation_tag`).
- **TIFF** (Pillow turns it upright on open and drops the tag): the grid is the upright
  image; the chain adds no tag step.
- **A TIFF whose tag is not trusted:** pagekit undoes Pillow's turn, so the grid is the
  stored pixels.

pagekit checks, file by file, whether Pillow applied the tag, so the tag is applied exactly
once whatever the Pillow version. The record's `applied_by` (`image library on open` or
`chain`) and `grid` say which. A tag outside the eight values is flagged and the source taken
as stored. `trust_orientation_tag` (default 1) controls trust; an overrides line
`{"source": ..., "step": "tag_trust", "value": false}` ignores one source's tag.

### A grey main page

`--output-mode grey` makes the pages of that run's sources grey (8-bit, lossless); `source`
(the default) keeps the source's colour mode. The choice is recorded with those sources and
kept on later runs. One page: `{"source": ..., "step": "output_mode", "page": 1, "value":
"grey"}` (optionally `"lock": true`).

The grey page goes through the same chain as the colour page and is then converted pixel by
pixel: no flattening, tone curve or sharpening. When every source pixel has equal channels
the common channel is kept and the conversion is exact. Otherwise `grey_rule` (`--grey-rule`)
decides: `luminance` (ITU-R BT.601: 0.299 R + 0.587 G + 0.114 B) or one of `red`, `green`,
`blue`.

Before a colour page is made grey, pagekit measures colour on the written page against the
page's own paper colour. Coloured marks clearly above the paper's noise
(`colour_chroma_margin`) over at least `colour_min_area_mm2`, ignoring specks under
`colour_speck_mm2`, flag the page ("this page holds colour that grey would remove") and keep
it in colour, unless grey was set by hand or locked. An ink that only loses the paper's tint
(black or faded brown on yellowed paper) does not count, while pale blue on cream does. Not
detected: colour fainter than the margin, specks below the size limit, a colour covering more
than half the paper, and differences between inks less saturated than the paper in its own
hue; such inks may merge into one grey level, so keep the page in source mode when that
matters. The manifest's `output_mode` records the mode, the choice and who made it, the rule,
exactness and the colour measure.

### Margin, padding and density

The **margin** keeps photographed paper around the content box (`margin_mm`, held to the
page box plus `margin_allowance_mm`). It is a setting, not a detection.

**Padding** (default none) adds a band of the page's paper colour around the finished page:
`--padding 4mm` (per axis via the resolution; a source with no resolution gets none, with a
flag) or `--padding 20px` (`padding_mm` or `padding_px`). It is the last link of the geometry
chain (`pad`), so point maps include it. The manifest's geometry records `margin_box` and
`regions` (canvas, padding, content area, the `photographed` polygon and the fill).

**Density.** Output density tags come only from a resolution the project holds. An overrides
line `{"source": ..., "step": "density", "page": 1, "value": [600, 600]}` sets a nominal
density without changing pixels; one that changes the axis ratio (beyond 0.2%), or one for a
source with no resolution, is refused and nothing is written.

**Resolution.** Millimetre settings need the scan resolution. A missing or implausible
resolution with no override is a flag, never a silent default, and those settings then apply
as 0 px. `--dpi 300` gives every source without a resolution 300 dpi (stored with origin
`override`; a source's own resolution is never replaced). Unequal axes are flagged and
converted per axis; the page is not resampled to equal axes.

### Geometry and output

For each page the chain is: quarter turn, the page's polygon on its side of the cut (kept
`overlap_mm` past it), rotation about the page's centre, crop to the margin box, scale, and
padding. The page is made from the original in one resampling: an exact crop with no
rotation, bicubic interpolation with one, and area averaging when shrinking (a rotated page
shrunk by up to 1/k is sampled on a k-times finer grid and averaged, one filter on the
original). Everything outside the page's polygon is filled with the page's paper colour (the
median of each band over pixels above Otsu's threshold).

Pages are written as `<source stem>_p<page>.tif` (or `.png`), keeping grey or colour, with
their resolution. TIFFs are written by pagekit's own writer (`pagekit/_tiff.py`).

Coordinates are continuous: pixel (i, j) covers [i, i+1) x [j, j+1). A point on a prepared
page maps back to the source through `affine_output_to_source` `[a, b, c, d, e, f]` as
(a x + b y + c, d x + e y + f); `pagekit.geometry.Chain` maps points and polygons both ways.

### The review sheet

`review.html` sits beside the manifest: one self-contained file (no internet, scripts or
outside fonts). A table at the top lists every scan, most flagged first. For each scan it
shows a preview of the upright original with the cut (vermilion), page boxes (blue) and
content boxes (green), and a preview of each prepared page; for each step, the value, where
it came from, the confidence, the evidence, every flag, and the exact overrides line to
change it. Previews are small JPEGs (`preview_long_side_px`, 320 px) loaded as you scroll, so
a sheet of hundreds of scans stays light.

To correct a page from the sheet: copy the line under the step, change the value, put it in
`overrides.json` in the output folder, and paste the one-line command the sheet prints
(written with full paths, the Python that made the sheet and `PYTHONPATH`, so it works from
any folder). Only what depends on the change is redone. The command is for macOS and Linux
shells.

### The stage cache

By default a folder beside the output folder named after it (`prepared.pagekit-cache` for
`prepared`); `--cache DIR` puts it elsewhere (never inside a source or output folder; a cache
belonging to another output folder is refused); `--no-cache` writes none. Per source (by
sha256) it holds small PNG previews of each step; `--cache-full` (`stage_cache_full`) also
keeps full-resolution lossless TIFFs. `index.json` keys each entry by the source's sha256 and
the step's inputs, so an unchanged re-run writes nothing; damaged or orphaned entries are
rewritten or removed. Each run prints the cache's size and warns when free disk looks short.
No prepared page is made from the cache; delete it any time.

### Steps and where their values come from

Each step value carries its origin (`detected`, `manual` or `locked`), a confidence (none
when set by hand), a sentence of evidence, flags, and an inputs hash (the source's sha256,
earlier steps' values, the settings the step read, and the sha256 of every pagekit file its
detector reads, settings and code). On a re-run:

- a detected value is recomputed when its inputs hash changes, and kept otherwise;
- a manual value is kept; if what it was set on has changed, it is flagged for a check.
  Applying the same correction again does not clear the flag; lock it (`"lock": true`) or
  give a new value;
- a locked value is kept and never flagged for that.

A hand-set value is never detected again; its detector still runs, and if it is confident
and far off (the `compare_*` settings), the evidence says so (a note, never a flag). Hand-set
values on a page that no longer exists (a spread set back to one page) stay in the project
under `dropped_pages`, flagged on every run, and return if the page does; delete them from the
project file to discard them.

`--report-stale` lists the steps that would change and why, without running anything (exit 1
when something is stale, 0 when nothing is).

After every page has its values, pagekit compares each page's skew, content size and margins
with the batch (median and median absolute deviation, with a floor on the spread) and flags a
page far from the middle. This needs at least `volume_min_pages` pages; blank pages are left
out.

### Corrections

Correct a value with an overrides file and `--overrides`:

```json
{"schema": "pagekit-overrides.v1", "overrides": [
  {"source": "scans/0012.tif", "step": "split",
   "value": {"pages": 2, "cut": [[1510, 0], [1532, 4480]]}},
  {"source": "scans/0012.tif", "step": "skew", "page": 2, "value": -0.6, "lock": true},
  {"source": "scans/0013.tif", "step": "content_box", "page": 1, "value": null},
  {"source": "scans/0014.tif", "step": "resolution", "value": [400, 400]}
]}
```

`source` is a path relative to the overrides file, or the source's sha256. Pages count from
1. Values:

| Step | Value |
|---|---|
| `orientation` | 0 to 3 quarter turns clockwise |
| `split` | `{"pages": 1}` or `{"pages": 2, "cut": [[x, y], [x, y]]}` in the upright frame's pixels |
| `skew` | degrees counterclockwise, under 45 |
| `page_box`, `content_box` | `[left, top, right, bottom]` in the levelled page's pixels, right and bottom exclusive; `content_box` `null` for a blank page. A box outside the levelled page is flagged |
| `margin` | millimetres |
| `resolution` | `[x_dpi, y_dpi]` |
| `tag_trust` | true or false |
| `crop` (per page) | `"none"`, `"page"` or `"content"` |
| `output_mode` (per page) | `"source"` or `"grey"` |
| `density` (per page) | `[x_dpi, y_dpi]` |

An entry may add `evidence`, a sentence saying why. Values are set as manual, or locked with
`"lock": true`. An override naming a source, page or step that does not exist is refused
(exit 2) and nothing changes. Run `prepare` again over the same output folder to continue the
project: every hand-set value is kept.

### The project file

`pagekit-project.v1`, at `OUTPUT/pagekit-project.json` unless `--project` names another, and
continued from on the next run. A closed JSON object: the settings, and for each source its
relative path, sha256, size, mode, resolution and its origin (`file`, `override`,
`missing`), source flags, orientation and split values, each page's output name and values,
and `dropped_pages`. The same inputs give the same bytes; it is written atomically. A project
naming a source not given to the run is refused; with no sources given, the project's own are
used.

### The manifest

`OUTPUT/pagekit-prepare.json`, schema `pagekit-prepare.v1`, closed. For each page: the
source's name and sha256; the output's name, sha256, size, `pixels_sha256` (of the decoded
pixels), format, mode, pixel size and resolution; the source resolution and origin;
`upright_resolution`; the geometry chain with both composed affine maps and the fill colour;
`orientation_tag`, `output_mode`, `density` (null unless set); every step's value with
origin, confidence, evidence and flags; and the verdict, `review` or `no_flags`. Also:
`batch` (the volume-wide comparison), `review` (the sheet), `skipped` (each with `name`,
`path`, `sha256` or null, and `reason`), `stale_outputs` (output files of pages that no longer
exist, left in place, never deleted), `thresholds`, `thresholds_measured` and
`thresholds_note`.

### What is identical, and where

- **Everywhere**: the decoded pixels of every prepared page and tone view (`pixels_sha256`),
  every manifest and project value other than a compressed file's sha256 and size, and the
  geometry (to the last bit of floating-point rounding). The tests pin these across
  platforms.
- **Byte for byte, on the same platform and library versions**: the page and tone-view files,
  manifest, project file and review sheet. zlib and Pillow's encoders may differ between
  builds, so the manifest records both the file's sha256 and its pixels' sha256.

### The grey tone view

`--tone-view` also writes a grey tone view (`pagekit/tone.py`) beside each page as
`<page>_tone.tif`, recorded in the manifest with its settings. A view is never written over a
prepared page or a source. The prepared page itself is unchanged.

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

`source` is the file name or sha256; every other key is optional. The cut is in the upright
image's pixels; each content box is drawn on the upright image after levelling it by that
page's true skew; `null` is a blank page. For each step it reports how many were right (within
the `measure_*` tolerance and not flagged), wrong and not flagged (the errors that matter
most, listed by name), and sent to review, with the error sizes. It changes no setting. Exit
0, or 2 when a file cannot be used.

### Settings

`thresholds_prepare.toml` holds the settings of `prepare` and `measure` (overlap, margin,
allowance, plausible resolution range, shrinking, output format, working resolutions,
comparison with hand-set values, batch checks, preview size, `measure` tolerances). The
detectors' own settings are in `thresholds_split.toml` and `thresholds_skew.toml`. All are
starting guesses with status `UNMEASURED`, which the review sheet says: until they are
measured with `measure`, a flag means "look at this page" and no flag is not proof the page
is right.

### Limits

- Only the orientation tag is read from metadata; colour profiles and other tags are not
  applied.
- Source modes handled: greyscale, colour, bilevel (written as greyscale) and palette
  (greyscale when every colour it uses is grey, else colour). Others are skipped.
- pagekit never deletes output files; stale ones are listed.

### For other code

`pagekit.answer` defines a detector's answer (value, confidence, evidence, flags) and refuses
one that breaks the shape. `pagekit.prepare.plan(..., detectors={step: Detector(method, run,
settings, compare)})` calls each detector in step order with a `StepContext` (earlier values,
settings, the original image, and `working_copy(long_side)`) and stores its answer;
`pagekit.pipeline.DETECTORS` are the connected detectors (with none given, every step takes
its neutral default with a flag); `pagekit.output.execute(plan)` writes the pages, manifest,
project file and review sheet.

## Checking a crop

```sh
python -m pagekit check --master scan.tif --crop 120,90,2480,3400 [--crop ...] \
    [--split-x 2510] [--min-short-side-px 1200] --json
```

A check on the cropped page alone cannot see writing that was cut off cleanly, so `check`
compares the declared crops with the uncropped master. Every flag means "send this page to
review"; it never changes a crop.

Crop boxes are `x0,y0,x1,y1` in the master's pixel grid as Pillow opens the file (see "The
orientation tag"), right and bottom exclusive. Exit status: 0 nothing flagged, 1 send to
review, 2 the inputs cannot be checked (including any read failure and unhandled modes such
as 16-bit grey `I;16`, which is refused rather than reduced to 8 bits).

The master is reduced to grey and split into ink and paper with Otsu's threshold, after
nearly-all-dark border rows and columns (scanner backdrop) are trimmed. Lone ink specks are
cleared; a one-pixel pen stroke is kept. A page with no ink contrast is sent to review, never
passed as `no_flags`. The checks:

1. **Ink discarded**: ink inside the page but outside every crop (count, share, bounding
   box), such as a margin note left out.
2. **Ink cut at an edge**: positions along each crop edge with ink just inside and just
   outside.
3. **Split against the gutter**: with `--split-x` or two side-by-side crops, the split's
   distance from the lowest-ink column run near the middle, and the ink in the split column.
4. **Resolution**: missing DPI, or a short side below the minimum.

The report, `pagekit-crop-check.v1`, is a closed JSON object (`schema`, `tool`, `input`,
`page`, `checks`, `flags`, `verdict` `review` or `no_flags`, `thresholds`,
`thresholds_measured`, `thresholds_note`), byte-identical for the same master and crops.
Thresholds live in `thresholds.toml`, all `UNMEASURED`.

Limits: the page area is found only by trimming dark borders (a pale backdrop, a partial
shadow or a neighbouring page counts as page); the ink threshold is global (uneven light,
faded ink or show-through can mislead it); specks of two or more touching pixels count as
ink; a dark gutter shadow, skewed spread or page with no blank gutter can mislead the gutter
search; crops are axis-aligned boxes.

## Licence and the clean-room rule

pagekit is Apache-2.0. ScanTailor and ScanTailor Advanced are GPL-3.0, and pagekit keeps a
wall between their code and ours:

- Build-side agents never read ScanTailor or ScanTailor Advanced source code, or any other
  GPL page-processing code, and never reproduce it.
- A separate reader agent may compare ScanTailor's behaviour with pagekit's and report only
  plain-language findings: the situation, the general technique and the settings that
  matter. Never code, and never "copy this".
- The build side works from those reports only, with published methods and general
  image-processing knowledge.
- The credit stays in `NOTICE`.

The full protocol, the checks that enforce it and the record are in
[cleanroom/CLEANROOM.md](cleanroom/CLEANROOM.md).
