# Finding: separate identities for source, recipe, pixels, interpretation and file

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

To know what was accepted, several different things need their own identity: the source bytes (and the frame within a multi-frame file), the resolved recipe of all step values, the actual page pixels, how those pixels are to be interpreted (grey or colour, profile, orientation, density), and the encoded file. A file hash mixes compression layout and tag order with the pixels, and a memory hash would mix in row padding.

## Pagekit's observed behaviour

Already: the source's hash, each step's inputs hash, the output file hash and a decoded-pixel hash are all recorded.

Differently: the decoded-pixel hash is not described as a versioned, defined serialization, and interpretation is not given its own identity separate from the file.

Not yet: a written, versioned definition of the canonical sample form; an identity for the interpretation; acceptance bound to the canonical pixels and interpretation together with the recipe, the actor and the revision.

## General technique

Define a canonical serialization of the raster: a version, the dimensions, row order and channel order, sample type and precision, byte order where it matters, and a normalized statement of interpretation; exclude row padding, object memory, compression layout and tag order. Hash that serialization. Two files with different compression but identical canonical rasters then share a pixel identity, and any change to samples or meaning changes it. Keep a small first profile (one frame, eight-bit grey or colour, normalized orientation, explicit colour assumptions) and refuse other modes explicitly. Hashes identify; they do not prove that processing preserved meaning.
Source: general knowledge

## Settings in general terms

- The profile version: changes whenever the serialization rules change.
- Supported sample forms: kept small at first and widened only by explicit decision.
