# Brief 0040: correction to brief 0039: tags on the grid Pillow opens

- Role: build side.
- Issued by the host session at 2026-10-04T17:27:07Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  6bb69ded51e611bcbe116a7ab098736ae87bdc087a5a5d46de4636fbb25e0577
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Correction to brief 0039, item B1 only; everything else in brief 0039 stands. Do B1 this way instead of undoing Pillow's transpose.

The main pipeline opens every scan through Pillow too, and for a TIFF it therefore sees the pixels Pillow has already turned upright. If pagekit described the stored, unturned pixels, its records would no longer match what the pipeline opens; for a half-turn tag the sizes would still match and a page could be cut a half turn off without any refusal. So pagekit must work on the source exactly as Pillow opens it, and apply a tag only when Pillow has not already applied it.

Fix: define pagekit's source grid as the pixels Pillow gives after loading. Before loading, read the tag; after loading, determine whether Pillow applied it (for example, it is gone from the loaded image, or the loaded size is the transposed stored size). If Pillow applied it, record the tag value with "applied on open by the image library" and add no tag step to the chain; if Pillow did not (as for JPEG and PNG today), apply it once in the chain as now. The trust setting: for a carrier Pillow turns on open, an untrusted tag means undoing Pillow's turn so the source is taken as stored; say so in the record. source_size is the size of the grid the chain starts from, and the point maps lead back to that grid; say in the README and the manifest which grid that is for each carrier. Tests: the eight-tag test with TIFF, JPEG and PNG carriers, each pixel-identical to the same page stored upright, with the record saying who applied the tag; an untrusted tagged TIFF taken as stored; and a check, by reading the image with Pillow the same way, that source_size equals the size Pillow opens. Do not depend on a particular Pillow version's behaviour silently: if a future Pillow stops turning TIFFs on open, the detection above must still give one application.
