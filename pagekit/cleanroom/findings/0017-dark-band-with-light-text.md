# Finding: a dark band that carries light writing or is part of the page, not a shadow

Reader brief sha256: 5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d

## Situation

Large dark areas are usually shadows or backdrop and are removed before content is chosen. Some are not: a dark printed header with light lettering, a dark stamp, a heavy ink blot, a seal or a heavily stained region. The document records that its reference tool re-examines dark areas that do not touch the frame border, inverts them, looks for text inside, and keeps any that contain text.

## Pagekit's observed behaviour

Not built yet. Pagekit's backdrop trimming removes only whole border rows and columns that are nearly all dark, so an interior dark area is counted as page and as ink.

## General technique

Separate dark areas by how they relate to the frame. Dark material connected to the frame border is most likely shadow or backdrop. Dark material wholly inside the page is more likely part of the document: invert it and test for text or structure inside; keep it as content if it carries writing or is surrounded by writing. Report what was removed, so a reviewer can see it.
Source: general knowledge

## Settings in general terms

What counts as a large dark area depends on page size and stroke width. The test for writing inside an inverted area depends on the writing size. Whether interior dark areas are ever removed automatically, or always kept, is a policy that should lean toward keeping them for archival registers.
