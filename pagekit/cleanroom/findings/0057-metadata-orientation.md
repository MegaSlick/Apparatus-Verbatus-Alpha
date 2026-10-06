# Finding: apply orientation metadata exactly once, including mirrored cases

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Some captures carry an orientation tag saying the stored pixels should be turned, or turned and mirrored, to display upright. If the tag is ignored, the page enters preparation sideways; if it is applied twice (once by a library, once by the tool), the page ends up wrong. A person may then add a further turn.

## Pagekit's observed behaviour

Already: orientation is a detected or hand-set number of quarter turns, recorded with its origin.

Differently: orientation tags are ignored; sources are taken in their stored pixel grid, and the crop check uses that grid too.

Not yet: reading the tag and normalizing all its cases, including the mirrored ones, exactly once; recording the operator's further turn separately from the tag.

## General technique

The common image metadata standard defines eight orientation values: the identity, three rotations and four cases with mirroring. Each maps to a fixed transform of the stored grid. Apply the tag's transform once to produce the upright source, record that it was applied, strip or reset the tag in the output, and keep any operator turn as a separate step after it. Tests use one synthetic image for each of the eight values.
Source: Camera and Imaging Products Association and JEITA, Exchangeable image file format for digital still cameras: Exif version 2.32, CIPA DC-008, 2019

## Settings in general terms

- None numeric. Whether the tag is trusted should be a recorded choice per source, since some capture software writes wrong tags.
