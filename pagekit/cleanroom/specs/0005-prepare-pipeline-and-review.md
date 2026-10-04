# Spec 0005: the prepare pipeline and the review sheet

Written by the host before any code, from specs 0002 to 0004, finding reports 0011,
0033 and 0035, and general knowledge. Nothing in it comes from ScanTailor, ScanTailor
Advanced or any other GPL program.

## Purpose

Connect the detectors of specs 0003 and 0004 to the core of spec 0002, so that one
command prepares a folder of scans, and give a person a way to see every decision and
correct the wrong ones without reading code or JSON.

## The pipeline

`python -m pagekit prepare` runs, for each source image, in this order:

1. orientation (spec 0003) on the source;
2. page count and split (spec 0003) on the upright frame;
3. for each page: skew (spec 0004) on the page's side of the cut;
4. for each page: page box, then content box (spec 0004) on the levelled page;
5. the margin and the geometry chain (spec 0002), applied once to the original;
6. the output images and manifest (spec 0002).

Each detector's answer is validated and stored with origin detected and its inputs hash.
A step whose value is manual or locked is not detected again; the detector may still
run to compare, and a large difference between the detected and the manual value is
reported in the evidence, not as a change. The neutral defaults of spec 0002 remain only
for a step whose detector raises an error: the error is caught for that page alone,
recorded as a flag naming the step and the error, and the batch carries on. One bad
page never stops a batch, and never passes without a flag.

## Volume-wide checks

After every page has its values, compare each page's skew, content-box size and
margins with the rest of the batch (finding 0011): compute the median and the median
absolute deviation, with a floor on the deviation, and flag pages that lie far from the
centre. Only when the batch has enough pages for this to mean anything. The flag
names the measurement and how far off it is.

## The review sheet

`prepare` also writes `review.html` beside the manifest: one self-contained HTML file
with no network access, no scripts from elsewhere and no external fonts, that opens in
any browser on a laptop or phone.

- It lists every source image, flagged ones first, sorted by how many flags they carry.
- For each source: a small preview of the original with the cut line and each page's
  page box and content box drawn on it in clear colours, and a small preview of each
  prepared page.
- For each step: the value, its origin, its confidence, the evidence sentence and every
  flag, in plain words.
- Under each step, the exact lines to put in the overrides file to change it, ready to
  copy, with the page and step already filled in.
- Previews are small reduced copies embedded in the file; the sheet never embeds a
  full-resolution image, so it stays small.
- A note at the top says which thresholds are not yet measured, and that until they
  are, a flag means "look at this page" and no flag is not proof the page is right.

## Measuring success (finding 0035)

`python -m pagekit measure` compares a prepared batch with a hand-checked answer file
(`pagekit-gold.v1`: for each source, the true orientation, page count and cut, skew, and
a content box) and reports, per step, how many pages were right, wrong or sent to
review, and the size of each error. It does not change any threshold. It is how the
thresholds will later be measured; until a gold set exists it is tested on synthetic
pages only.

## Behaviour the tests must pin

On synthetic pages only:

- A batch with an upright single page, a sideways page, an upside-down page, a spread
  with a fold, a tilted page and a blank page is prepared with the right turns, page
  counts, angles and boxes, and the blank page is kept as an image of the paper.
- A detector that raises an error on one page flags that page and the batch finishes.
- A manual value is not re-detected, and a large disagreement is reported in the
  evidence.
- A page whose skew is far from the rest of a large batch is flagged; a small batch is
  not compared.
- The review sheet is one file, opens without network, lists flagged pages first, and
  its override lines, pasted into an overrides file, change exactly that step.
- `measure` reports right, wrong and review counts against a gold file.
- The same input gives byte-identical outputs, manifest and review sheet.

## Not in this slice

Dewarping, binarisation and other image cleaning, colour calibration, an interactive
editor, and wiring prepared pages into the main pipeline (that is the lead's decision).
