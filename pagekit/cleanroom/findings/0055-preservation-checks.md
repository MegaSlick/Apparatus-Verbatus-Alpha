# Finding: four separate preservation checks, with coverage counted as a union

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Showing that a prepared set keeps the source's evidence needs more than one test. Two pages that overlap across the fold may together appear to cover more than the source if their areas are simply added. A note split down the middle between two pages is present in pixels but not readable as a whole. A faint stroke inside a kept area can still vanish in rendering or encoding. And a colour copy kept only for reference does not help readers who are given the grey page.

## Pagekit's observed behaviour

Already: the crop check counts ink outside every crop and ink cut at an edge; the split keeps an overlap past the cut so a stroke across the fold is whole on at least one page; components crossing the cut are counted and flagged if they overhang by more than the overlap; the content box keeps margin notes and signatures; the chain maps every output point back to the source.

Differently: coverage is assessed per check rather than as a union of kept source regions after the actual output clipping; no rendered-signal or documentary-context check is described.

Not yet: union coverage of kept source regions after the real output canvas clipping; a test of the actual render and encode path on faint detail; a check that each protected note or signature appears whole, with useful surrounding context, in at least one delivered page; counting only pages actually delivered to readers.

## General technique

Geometric coverage is computed by mapping each output page's kept domain back to the source and taking the union (overlaps counted once), then subtracting from the source domain to find what no page keeps. Signal survival is measured on synthetic faint strokes put through the same path. Context is checked by testing that each protected region's bounding area lies wholly inside at least one delivered page. Each check reports separately; passing one does not imply another.
Source: de Berg, Cheong, van Kreveld and Overmars, Computational Geometry: Algorithms and Applications, Springer, third edition, 2008 (polygon union and overlay)

## Settings in general terms

- Faint-stroke test levels: should depend on the faintest ink contrast seen in the collection.
- Context around a protected mark: should depend on the size of the mark and a reader's need to see its neighbouring text, for example about one line height of text around it.
