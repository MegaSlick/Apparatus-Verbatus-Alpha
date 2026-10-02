# Spec 0001: the crop check

**Written after the code, from the built behaviour and its tests.** Slice 1 was built
before the clean-room process had a spec step. Future slices write the spec first,
commit it, and only then brief a build-side agent on it. Sources for this spec:
pagekit's README, its tests, and Otsu (1979). Nothing in it comes from ScanTailor or
any other GPL program.

It was revised when an independent review of the branch changed three behaviours: a
page with no ink detected now goes to review, lone specks are cleared instead of
median-filtering the ink map (which had erased thin pen lines), and any failure to read
the master is exit status 2. The review's points were about pagekit's own behaviour;
none came from another program.

## Purpose

A scanned master is cropped (and sometimes split into two pages) before anything
downstream reads it. If the crop cuts off writing, the cropped page shows no trace of
it. The crop check compares each declared crop with the uncropped master and sends the
page to review when writing may have been lost. It never changes a crop and never
writes the master.

## Inputs

- One master image, a single frame, read in its stored pixel grid (no rotation from
  metadata).
- One or more crop boxes, each given as left, top, right and bottom in that grid, the
  right and bottom edges not included. Every box must be non-empty and inside the
  image.
- Optionally, a split position: a column where a two-page spread is divided. If none is
  given and there are exactly two crops side by side that share most of their height
  and do not overlap by more than half the narrower one, the split is taken as midway
  between their facing edges.
- Optionally, the minimum acceptable short side in pixels.

An input that breaks these rules is refused with a plain message and exit status 2;
nothing is measured.

## Measuring the page

1. The image is reduced to grey.
2. Ink and paper are separated with Otsu's threshold on the grey histogram (N. Otsu,
   "A Threshold Selection Method from Gray-Level Histograms", IEEE Trans. Systems, Man,
   and Cybernetics, SMC-9(1):62-66, 1979).
3. Rows and columns at the image's borders that are nearly all dark are treated as
   scanner backdrop and trimmed off; what is left is the page.
4. The threshold is computed again from the page alone. If the dark and light sides
   differ by less than a set contrast, the page is treated as carrying no ink.
5. Ink pixels with no ink among their eight neighbours (lone specks) are cleared from
   the ink map. A stroke one pixel thick is kept.
6. If no ink is detected, the page is sent to review with the reason that the checks
   could not run; it is never passed without flags.

## The four checks

Each check reports its measurements, a yes-or-no flag and plain-language reasons. Any
flag means "send this page to review".

1. **Ink discarded.** Ink inside the page but outside every crop: the pixel count, its
   share of all the page's ink and the box around it. Flagged when either the count or
   the share reaches its threshold.
2. **Ink cut at an edge.** For every edge of every crop, a thin band just inside and a
   thin band just outside, each clipped to the page. Positions along the edge with ink
   in both bands are counted and reported as runs. Flagged when the count reaches its
   threshold. An edge on or beyond the page's border is not compared.
3. **Split against the gutter.** Only when there is a split. The share of ink in each
   column of the page is computed, and the lowest-ink run of columns in a window around
   the middle is taken as the gutter (the widest such run; ties go to the one nearest
   the middle). Flagged when the split lies outside that run by more than a tolerance
   proportional to the page's width.
4. **Resolution.** Pixel size and DPI. Flagged when DPI is missing or the short side
   is below the minimum.

## The report

A closed JSON object with schema name `pagekit-crop-check.v1`: the input (file name,
sha256, size in bytes, crops, split), the page measurements, one entry per check, the
list of flags, a verdict (`review` or `no_flags`), the thresholds used and whether each
was measured, and a plain note on what an unmeasured threshold means. The same master
and crops give byte-identical JSON wherever the file sits. Exit status: 0 for no flags,
1 for review, 2 for unusable input.

## Thresholds

Every threshold is a starting guess kept in one table, each marked as not yet measured
on real pages, and the report says so. Until they are measured, a flag means "look at
this page" and no flag is not proof the crop is right.

## Behaviour the tests pin

- A crop inside the margins of a clean page raises no flag.
- A margin note left outside the crop is flagged as discarded ink.
- A line of text cut by a crop edge is flagged at that edge.
- A split on the gutter of a two-page spread raises no flag; one well off it is
  flagged.
- A master without DPI, or with a short side below the minimum, is flagged.
- A dark scanner backdrop around the page is not counted as page or as ink.
- A blank page has no ink detected and goes to review for that reason alone.
- Thin pen lines outside the crop are counted as discarded ink; lone specks are not.
- A 16-bit greyscale master, or any master that cannot be read, is exit status 2.
- Two runs on the same input give byte-identical reports and leave the master
  untouched.
- The command line's JSON round-trips and its exit statuses are 0, 1 and 2 as above.

## Known limits

The page is found only by trimming dark borders; the ink threshold is global; the
speck clearing keeps specks of two or more touching pixels; the gutter search can be
misled by a dark gutter shadow, a skewed spread or a page with no blank gutter; crops
are axis-aligned boxes; nothing proposes or corrects a crop.
