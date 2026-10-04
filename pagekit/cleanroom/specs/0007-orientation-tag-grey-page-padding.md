# Spec 0007: the orientation tag, a grey main page, and padding apart from margin

Written by the host before any code, from finding reports 0036 to 0039, 0045, 0049, 0056
and 0057, specs 0002 to 0006, and general knowledge. Nothing in it comes from ScanTailor,
ScanTailor Advanced or any other GPL program.

## Purpose

Three small additions to `prepare`, each one a gap the findings name:

1. a scan's orientation tag is applied exactly once, before anything else;
2. a person can choose a plain grey page as the main prepared page, as a reviewed choice;
3. blank padding added around a page is a separate setting from the photographed margin
   kept around the writing.

Defaults do not change what a reader receives: a scan without a tag, with no grey choice
and no padding, gives exactly the same page, bytes and manifest values as before, except
for the new fields that record these choices.

## 1. The orientation tag (finding 0057)

Some image files carry a tag saying how the stored pixels must be turned, or turned and
mirrored, to show the picture upright. The common photographic metadata standard defines
eight values: as stored, three quarter-turn rotations, and four cases that include a
mirror. (CIPA DC-008, Exif 2.32, 2019.)

- pagekit reads the tag from the source and applies its transform once, as the first link
  of the geometry chain, before the orientation step. The orientation detector, the
  split and every later step see the tag-corrected frame. A person's quarter turns stay a
  separate step after it.
- The tag's transform is exact (no resampling) and is stored in the chain as plain
  parameters, so the point maps of spec 0002 cover it and a point maps back to the stored
  pixels of the source.
- The output carries no orientation tag, or the value meaning "as stored", so no reader
  applies it a second time.
- A tag value outside the eight is a flag, and the source is taken as stored. Whether to
  trust the tag is a setting per source, default trust, because some capture software
  writes wrong tags; a source whose tag is not trusted says so in its evidence.
- The record names the tag value found, whether it was applied, and the transform.

## 2. A grey main page (findings 0036 to 0039)

Today the prepared page keeps the source's colour mode, and grey exists only as the
separate tone view of spec 0006. This adds a choice of output mode, per run and per page:
`source` (the default, as today) or `grey`.

- **Same geometry.** The grey page is made through exactly the same chain as the source-mode
  page, so the two differ only in sample values. It is a plain conversion with no
  flattening, no tone curve and no sharpening; the tone view of spec 0006 stays a separate
  view.
- **The grey rule** is named and recorded: luminance (default), or one named channel. On a
  grey source the choice changes nothing.
- **Equal channels are lossless.** pagekit checks whether every decoded pixel of a colour
  source has equal channels. When they are exactly equal, the grey page takes the common
  channel, which keeps every intensity value unchanged, and the record says the
  conversion was exact. When they are not, any rule changes values; the record says the
  conversion was a reviewed one.
- **Colour evidence is checked, not assumed** (finding 0037). For a colour source whose
  channels are not equal, pagekit measures how far its pixels sit from the neutral axis
  (chroma), against the chroma noise measured on the page's own plain paper. When marks
  carry clearly more chroma than that noise over an area at least the size of a small
  mark, a grey choice on that page gets a flag ("this page holds colour that grey would
  remove") and the page is written in source mode instead, unless the person has set grey
  for that page by hand or locked it. The flag names where the colour lies. A source whose
  colour is only noise takes the grey choice with no flag.
- **A batch choice has a scope** (finding 0039). Choosing grey for a run applies to the
  sources named in that run and is recorded with them; it does not carry over to sources
  added later. A page in the batch with colour evidence is still flagged as above.
- **The review sheet** shows, for a page made grey, the rule, whether the conversion was
  exact, and any colour-evidence flag with an override line to keep source mode or to
  force grey.
- Outputs stay lossless TIFF (or PNG by setting), 8-bit grey for a grey page. The manifest
  records the output mode, the rule, exactness, and the colour measure.

## 3. Padding apart from the margin (finding 0056)

Today one margin setting keeps photographed paper around the content box and, where the
margin runs past the paper, adds fill. This separates the two.

- **Margin** keeps photographed source around the content box, clamped to the page box plus
  the allowance, as today. It never adds fill by itself beyond what the allowance already
  allows.
- **Padding** is a new setting, default none: a band of the measured paper colour added
  around the finished page, in millimetres (converted per axis with the resolution) or in
  pixels. It enlarges the canvas without changing the content scale or position relative to
  the source; the point maps include it.
- The manifest records, for each page, the margin box in source coordinates, the padding
  on each side, and which regions of the output are photographed source and which are
  fill.
- A margin of all available paper is a setting value (the page box), not a new mode; the
  default margin is unchanged pending the lead's decision.

## 4. Density on output (finding 0049)

- Output density tags are written only from a resolution the project holds, as today. If a
  person sets a nominal density for a page, the ratio between the horizontal and vertical
  values must equal the accepted ratio; a request that changes the ratio is refused with a
  plain message. pagekit never stretches pixels to make the axes equal.

## Behaviour the tests must pin

On synthetic pages only:

- Each of the eight orientation tag values on one synthetic page gives an upright frame
  that matches the same page stored upright, pixel for pixel; a mark maps back to its
  stored position; the output carries no tag that would turn it again; a person's quarter
  turn after the tag gives the expected result; an invalid tag is flagged and ignored; an
  untrusted tag is ignored and the evidence says so.
- With no tag, no grey choice and no padding, outputs and manifest values are identical to
  before, apart from the new recording fields.
- A colour source with equal channels made grey keeps every intensity exactly, and the
  record says exact. A near-equal source made grey says reviewed.
- A colour source with a red stamp or blue-black annotation, made grey, is flagged and kept
  in source mode; with grey set by hand, it is grey and the flag stays visible.
- A colour source whose only colour is sensor noise is made grey with no flag.
- Padding enlarges the canvas by exactly the requested amount on each side, keeps content
  scale, fills with the paper colour, and the point maps still round-trip.
- A nominal density that changes the axis ratio is refused.
- The same input gives byte-identical outputs, manifest and review sheet.

## Not in this slice

An interactive editor (finding 0041 and others; a later spec); manual tone controls on
the main page; changing the default margin to keep all paper; colour-to-grey methods
beyond a named rule; colour profile conversion; multi-frame sources.
