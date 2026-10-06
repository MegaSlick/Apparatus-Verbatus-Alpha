# Finding: simple everyday controls, with full provenance kept in an inspectable record

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The person works on a laptop and often a phone. Everyday work needs a few clear controls: split, upright, left and right angle, retain more source, add padding, source or grey, tone, export; plus a filmstrip of captures, linked views of the original and its pages, overlays for the split and the kept region, and undo. Hashes, profile assumptions and measurements must be available but should not crowd each interaction.

## Pagekit's observed behaviour

Already: the review sheet is a single offline file that opens on a laptop or phone, shows the original with the cut and boxes drawn, small previews of each page, and every step's value, origin, confidence, evidence and flags in plain words; full details sit in the manifest and project file.

Differently: the sheet shows every detail for every step at once, and correcting requires editing a file and running a command.

Not yet: interactive controls of the kind listed above; a bounded filmstrip; a view of omitted source; an advanced panel holding hashes, profile assumptions and measurements on demand.

## General technique

Progressive disclosure: show the controls needed for the common task, and put detail one step away where those who need it can open it. The record underneath stays complete regardless of what the screen shows.
Source: Nielsen, Usability Engineering, Academic Press, 1993

## Settings in general terms

- Filmstrip and cache size: should depend on device memory and screen size, with phones the smallest case.
