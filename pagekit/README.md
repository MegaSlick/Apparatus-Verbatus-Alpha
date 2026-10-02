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
