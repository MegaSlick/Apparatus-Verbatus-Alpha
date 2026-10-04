# Finding: a detector that cannot decide abstains, leaving zero and manual choices open

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

A skew or split detector finds no reliable answer: too little writing, conflicting cues, a page unlike the rest. The page must still be preparable: the person can take zero correction, type an angle, or draw the regions by hand.

## Pagekit's observed behaviour

Already: when unsure, each detector returns a neutral value (no turn, one page, zero skew, the whole page) with a flag and low or zero confidence; a detector error is caught for that page alone and the batch continues; a hand-set value is never re-detected.

Differently: an uncertain detector still writes a value with origin detected, and the page is prepared with it; abstention is expressed as a neutral value plus a flag rather than as no decision.

Not yet: an explicit abstain outcome distinct from a confident zero; a next action shown with it (take zero, enter a value, draw regions).

## General technique

Classification with a reject option: a decision is made only when the evidence clears a confidence level, and otherwise the case is passed to a person. Rejecting is better than a wrong answer when an error costs more than a review. The trade-off between error rate and reject rate is set by that cost. The rejected case keeps its neutral, harmless value (no rotation, no cut) so nothing is lost while it waits.
Source: Chow, On optimum recognition error and reject tradeoff, IEEE Transactions on Information Theory, 1970

## Settings in general terms

- The confidence needed to decide: should depend on the cost of a wrong cut or angle relative to the cost of a person's review, and be measured on hand-checked pages.
- The neutral fallback: always the value that changes nothing (zero angle, one page, the whole frame).
