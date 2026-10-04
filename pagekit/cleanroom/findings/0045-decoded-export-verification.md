# Finding: check the decoded exported file against the accepted page

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The file actually written may differ from the page the person accepted: a writer may change depth, add a palette, apply a different compression, drop or alter density or orientation tags, or the person may have accepted an older revision. A hash of the written file proves only that the bytes are those bytes, not that they decode to the accepted pixels.

## Pagekit's observed behaviour

Already: the manifest records each output's file hash and a hash of its decoded pixels, its format, mode, size and resolution; the same project gives byte-identical images.

Differently: the decoded-pixel hash is recorded, but the reports read do not describe comparing it against an independently identified accepted candidate before the page counts as done.

Not yet: a full decode of each export compared with the accepted candidate's samples and interpretation, covering dimensions, depth and layout, photometric meaning, orientation, colour profile policy, density and aspect metadata, frame count and allowed compression; a block on publishing a mismatch.

## General technique

Round-trip verification: after writing, read the file back with an independent decoder and compare its decoded samples and declared interpretation, field by field, against the accepted in-memory result. Any difference blocks publication as the accepted result. The comparison is exact for lossless formats. This is the standard read-after-write check used wherever silent corruption or format drift would matter.
Source: general knowledge

## Settings in general terms

- Sample comparison: exact, with no tolerance, for lossless output.
- Metadata fields compared: every field that changes how the pixels are interpreted; incidental fields (tag order, compression layout) are excluded.
