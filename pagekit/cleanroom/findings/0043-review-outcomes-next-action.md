# Finding: every review outcome names its cause and a next action

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A page can be held for very different reasons, and the person needs to know what to do about each: a detector could not find skew or a split; faint or edge writing may be lost; the source looks blank; the source itself is already clipped or covered; an arrangement is not supported; the file uses a depth or mode the tool cannot handle, or is corrupt; the exported pixels are wrong or belong to an older revision; a resource limit was reached.

## Pagekit's observed behaviour

Already: each flag is a plain-language reason; the review sheet lists flagged pages first and gives, under each step, the line to change it; unusable files are skipped with a reason and the rest of the batch continues; a blank page is still written; hand-set values are never silently dropped.

Differently: flags are free sentences grouped by step, without a fixed kind; detector uncertainty, a possible loss of evidence and a hard failure all lead to the same review verdict.

Not yet: named outcome kinds that separate detector abstention, a need for human review, a hard failure that cannot be waived, and a resource outcome; a stated next action for each kind; a recorded human decision attached to the outcome.

## General technique

Treat outcomes as a small closed set of kinds, each with an owner and an allowed resolution. A soft outcome (uncertainty) is resolved by a recorded human decision on the current evidence. A hard outcome (a broken invariant such as pixels that differ from the accepted candidate, or an unsupported conversion) cannot be turned into a pass by a decision; it is fixed by re-rendering, choosing another supported path or leaving the page pending. A suitable table of actions: no skew or split found, take zero or set by hand; possible loss, keep more source or reduce processing or use source mode; blank-looking source, keep it and optionally note it; source already clipped, record it and do not invent the missing part; unsupported mode, keep the source and report the limitation; wrong or stale pixels, re-render and re-check; resource limit, queue or reduce concurrency or create an explicit smaller candidate. A problem on one capture never blocks unrelated captures.
Source: general knowledge

## Settings in general terms

- None numeric. The set of kinds and their allowed resolutions should be fixed and documented, and every decision should record who made it, when and on which revision.
