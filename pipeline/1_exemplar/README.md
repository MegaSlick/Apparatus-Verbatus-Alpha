# Exemplar

The door turns each submitted source into an accounted Exemplar page, then this
stage seals it immutable. Filename, SHA-256, and byte-count linkage stays with the
record so an exported page can be matched back to the original source.

Decoder routing is by bytes, not extension. Single-frame rasters are decoded and
kept as their own bytes. PDF and every multi-frame raster (multi-page TIFF, animated
GIF, APNG, animated WebP, multi-picture JPEG) are fanned out and each page is
rendered exactly once at the door, as lossless PNG, or as TIFF where PNG cannot hold
the samples (signed 32-bit, float and some 16-bit layouts). PDF rendering paints the
whole page, including text and images together—never an embedded-image extraction. A
decoder gap is a named alarm about the pipeline, not a format-policy rejection.

Real submissions require the self-hashed local filename ledger created by
`operations/submit/submit.py`, and must sit inside an approved storage root. The ledger
is bound into `run.json`'s self-hash, checked again at the Exemplar/Designator
boundary, and carried to the Armarium export. No transfer, pod, or real source data
is part of this stage.

Read [CONTRACT.md](CONTRACT.md) for the artifact contract.
