# Finding: the scope of a person's review is a recorded policy, not proof of preservation

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A person cannot inspect every tile of every page at full resolution. Acceptance still needs to say what was looked at, and must not suggest that a look proves nothing was lost. Later, batch acceptance may rely on risk scores and spot checks.

## Pagekit's observed behaviour

Already: the review sheet states that thresholds are unmeasured, that a flag means look at this page and that no flag is not proof the page is right; flagged pages come first.

Differently: there is no acceptance step; review is implied by applying or not applying corrections.

Not yet: an acceptance record naming the revisions accepted, by whom, with which checks and which review scope (overview, boundaries, gutter, protected marks, spot checks); a rule that acceptance never carries over to a later re-render; reporting of missed risks and review burden alongside completion.

## General technique

Acceptance sampling and audit practice: state the inspection plan, record what was inspected and the result, and report the residual risk the plan leaves. A risk-ordered review queue puts likely problems first; random spot checks estimate what the ordering misses. Acceptance attaches to an exact identity and is void for anything that changes.
Source: Montgomery, Introduction to Statistical Quality Control, Wiley, seventh edition, 2012 (acceptance sampling)

## Settings in general terms

- Spot-check rate: should depend on the measured miss rate of the risk ordering and the cost of a missed loss.
- Default scope for the first manual flow: overview plus boundary, gutter and protected-mark detail.
