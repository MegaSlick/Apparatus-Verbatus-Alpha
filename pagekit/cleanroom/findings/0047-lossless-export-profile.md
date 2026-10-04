# Finding: lossless export profile with no silent codec or precision fallback

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Prepared pages are meant to be made once and reused. A container format that allows lossy compression, a writer that quietly reduces depth or makes a palette, or one that falls back to another codec when the chosen one is unavailable, can each lose detail without anyone noticing.

## Pagekit's observed behaviour

Already: pages are written as lossless TIFF with deflate compression, or PNG by setting; no lossy format is offered; pagekit's own writer gives the same bytes for the same pixels; sixteen-bit grey and other unhandled modes are skipped with a reason rather than reduced.

Differently: the reconciled documents propose a qualified LZW TIFF profile as the first default, with deflate and PNG qualified separately; pagekit already uses deflate. Either is lossless; what matters is that the chosen profile is named and qualified.

Not yet: an explicit refusal list in the export profile (lossy compression inside TIFF, unrequested palette or precision reduction, codec fallback); a held outcome for unsupported sizes or depths, or an explicitly chosen qualified alternative.

## General technique

Name each export profile (container, compression, sample form, required tags) and qualify it on each platform by writing and fully decoding test images. The writer either produces exactly that profile or stops with a named outcome; it never substitutes. Lossless compression schemes such as LZW and deflate reproduce samples exactly, so the choice between them is about compatibility with the receivers and speed, not quality.
Source: Adobe Systems, TIFF Revision 6.0 specification, 1992

## Settings in general terms

- The default profile: chosen for what the receiving systems read reliably.
- Size limits: the classic TIFF file size limit and the receivers' limits decide when a page needs a different qualified profile; the person chooses it explicitly.
