# Finding: separate development, calibration and untouched evaluation pages, grouped by relation

Reader brief sha256: bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2

## Situation

Automatic proposals and review priorities will have thresholds set on real pages. If the same pages, or pages from the same register, sheet, insert or recapture, are used both to set thresholds and to judge them, the result looks better than it is. A small sample with no misses does not show that rare serious errors are absent.

## Pagekit's observed behaviour

Already: every threshold is marked unmeasured; the measure command compares a batch with a hand-checked answer file and reports right, wrong (unflagged errors, listed by name) and sent-to-review counts and error sizes, and changes no setting; tests use synthetic pages only.

Differently: one answer file is used; the reports read do not describe splitting real pages into development, calibration and evaluation sets, or grouping related captures.

Not yet: three separate sets, with the evaluation set untouched until thresholds are fixed; grouping so related registers, sheets, recaptures and inserts fall in one set; reporting severe evidence loss, missed risks, held and failed cases, omissions and review burden; synthetic invariant tests kept apart from real-source evaluation; recognition gains measured separately with downstream readers fixed.

## General technique

Hold-out evaluation with grouped splitting: divide data into sets for building, tuning and final testing, and split by group (here the register or physical sheet) so near-copies never straddle sets. For rare errors, a sample of n cases with no failures still allows a failure rate up to about three divided by n at ordinary confidence, so zero observed loss in a small sample is not a guarantee.
Source: Roberts and others, Cross-validation strategies for data with temporal, spatial, hierarchical, or phylogenetic structure, Ecography, 2017; Hanley and Lippman-Hand, If nothing goes wrong, is everything all right?, Journal of the American Medical Association, 1983

## Settings in general terms

- Set sizes: should depend on how rare the errors of concern are and the confidence wanted.
- Grouping key: the physical register or sheet, and any recapture or insert relation.
- Operating points: chosen on the calibration set by the cost of misses against review burden.
