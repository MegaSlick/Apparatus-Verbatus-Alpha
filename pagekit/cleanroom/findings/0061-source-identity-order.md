# Finding: source identity by content and frame; capture order apart from reading order

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A file may be renamed or moved, and a container may hold several frames. The order in which frames were captured is not always the order in which the register is read, and left and right in a capture do not prove which side of a leaf it is.

## Pagekit's observed behaviour

Already: each source is identified by its content hash as well as its path, and overrides may name a source by either; received bytes are only read, never changed; output names follow the source name and page number.

Differently: multi-frame containers are not described; page numbering follows position after the split.

Not yet: frame identity as source identity plus frame index; a separate record of interpreted folio or reading order; wording that presents left and right as positions in the upright capture, not as recto and verso.

## General technique

Use content addressing: identify each source by a cryptographic hash of its bytes, and each frame by that hash plus its index. Keep capture order (as received) and reading order (as interpreted by a person) as two different fields, so correcting one never changes the other.
Source: general knowledge

## Settings in general terms

- None numeric.
