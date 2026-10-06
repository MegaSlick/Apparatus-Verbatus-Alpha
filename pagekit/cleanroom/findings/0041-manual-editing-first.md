# Finding: manual editing must work before, and without, automatic detection

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

The person's first priorities are splitting spreads, deskewing each page on its own, keeping generous margins and choosing grey or colour. These must be usable by hand from the start, in one flow: open a capture, set it upright, choose one page or a spread, adjust each page region so it includes everything, set each page's angle, choose kept margin and added padding, choose source or grey, optionally adjust tone, look at the actual result and export. Automatic proposals come later and only help.

## Pagekit's observed behaviour

Already: every step value can be set by hand through a corrections file, kept on re-run, locked, and only what depends on it is recomputed. With no detectors, every step takes a neutral default with a flag, so the chain runs on hand-set values alone.

Differently: correction is done by editing a small file and running a command printed on the review sheet, after the detectors have run; the flow is detection first, correction second.

Not yet: an interactive editor for these operations; a grey choice; separate controls for kept margin and added padding; a manual flow that does not first run detectors.

## General technique

Direct manipulation: the person acts on a visible representation of the page (a draggable cut, a page rectangle, an angle handle) and sees the effect at once, with every action reversible. Automatic detectors then become proposals placed into the same controls, which the person can accept or change. Building the manual path first gives a baseline that is correct by construction and a way to make the hand-checked answers later used to measure detectors.
Source: Shneiderman, Direct manipulation: a step beyond programming languages, IEEE Computer, 1983

## Settings in general terms

- None numeric. The order of controls should follow the order of the geometry chain, and each control should hold a value in the source's coordinates so it survives re-rendering.
