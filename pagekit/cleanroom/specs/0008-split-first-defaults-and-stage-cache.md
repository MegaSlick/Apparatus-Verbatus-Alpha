# Spec 0008: split-first defaults and a stage cache

Written by the host before any code, from the lead's direction of 2026-10-04, specs 0002
to 0007, and general knowledge. Nothing in it comes from ScanTailor, ScanTailor Advanced
or any other GPL program.

## Purpose

The lead has set pagekit's priorities: preserve the original, split left and right
properly, and handle colour. Cropping to the paper or to the writing is a later feature
that needs extensive testing before it is trusted, so it is off by default. A person also
needs to see what each step did.

## 1. Defaults

- **On by default:** the orientation tag, orientation, the split, skew, and the output mode
  of spec 0007.
- **Off by default:** the page box and the content box. With them off, a page is its side
  of the cut (with the overlap of spec 0002), levelled, on a canvas large enough to hold
  the whole levelled side, with nothing of the source cut away; the margin of spec 0002
  does not apply; padding of spec 0007 still applies when set.
- **Turned on** by a setting per run (`--crop page` for the page box, `--crop content`
  for page box and content box), or per page by an override. When on, they behave exactly
  as specs 0004 and 0005 say. The review sheet says on every page whether cropping was on.
- The detectors for the page box and content box may still run when cropping is off, to
  report in the evidence what they would have cut, so they can be tested on real pages; they
  never change the page when off. Whether they run when off is a setting, default off, so a
  default run is not slowed.
- The manifest records which steps were applied and which were off.

## 2. The original is preserved

Already required by spec 0002; this restates it as a test: a run never changes a source
file's bytes, its modification time, or anything else in the source folder.

## 3. The stage cache

`prepare` writes, beside the output folder, a cache folder holding for each source:

- the source as opened (after any orientation tag), and a thumbnail;
- after the quarter turn: the upright frame with the cut line drawn on a thumbnail;
- for each page: the page's side of the cut, and the levelled page, each as a full-
  resolution lossless image and a thumbnail;
- when cropping is on, the page box and content box drawn on a thumbnail of the levelled
  page.

Rules:

- **Cache images are for looking only.** No output page is ever made from a cache image;
  every prepared page is still made from the original source in one resampling (spec 0002,
  finding 0031). A test proves the prepared page is identical whether the cache exists, is
  stale or is deleted.
- **Each cache entry is keyed** by the source's sha256 and the inputs hash of the step that
  produced it, so a re-run reuses an entry whose inputs have not changed and rewrites one
  whose inputs have.
- **Lossless** for full-resolution entries (the TIFF writer of spec 0002); thumbnails may be
  small JPEG or PNG and are marked as previews.
- **Deletable:** removing the cache folder loses nothing; the next run rebuilds what it
  needs. A setting turns the cache off, and one sets its folder.
- The review sheet links to the cache thumbnails where they exist, so a person can open
  each step's image.
- The cache never goes inside the source folder.

## Behaviour the tests must pin

On synthetic pages only:

- A default run on a spread gives two pages, each its whole side of the cut, upright and
  levelled, with no source pixel inside that side cut away; the manifest says cropping was
  off.
- `--crop content` gives the pages specs 0004 and 0005 give today.
- A source file's bytes and modification time are unchanged after a run.
- The cache holds the entries listed above with the stated keys; a re-run with no change
  rewrites nothing; a changed skew rewrites only that page's entries; deleting the cache
  and re-running gives byte-identical prepared pages.
- A prepared page is the same with and without the cache.

## Not in this slice

A trusted text or layout detector for cropping (a later spec, after extensive testing or a
recognition-model aid); an interactive editor.
