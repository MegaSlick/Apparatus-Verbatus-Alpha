# Ink map — handoff

The ink map runs after the Exemplar seal and before the Designator. It writes one
`kind="ink-map"` record per sealed page, including zero-ink pages, with the shared
`common.residual_ink::page_residual_ink` result measured against empty coverage.

Each record carries `payload["page_ordinal"]`, the sealed page's ordinal from the
run's `source_manifest`. That ordinal is the identity a consumer joins on: this
document is the interface, and without it named here a consumer has to guess —
keying on `subject_id` instead silently mismatches records the day subject naming
changes, and a page's ink evidence lands against the wrong act. The stage refuses
the whole census rather than publish a record it cannot bind to exactly one
submitted source.

`payload["edge"]` is the bounded `unclaimed-edge-ink` detector: it measures only
the 64-pixel page perimeter using that same implementation. A flagged record is
unresolved evidence, not a hold. **Unit 14 owns the explicit hold outcome for an
unproposed cross-page half act.** The thresholds remain **PROPOSED, NOT YET
MEASURED**; this stage claims no calibration.

The Designator consumes this producer's completion seal before any detection. The
Recensor continues to use the same shared residual-ink implementation for its late
proposal/recovery coverage reconciliation.

## Two things Unit 14 must not misread

**`payload["ink"]["flagged"]` is not an alarm here.** This stage measures with
*empty* coverage, because before the Designator runs there is no coverage to
measure against. `flagged` in `common.residual_ink` means "outside-coverage ink
passed a gate", so with empty coverage it is true of every page carrying more
than 24 ink pixels, and `fraction_outside_per_million` is 1,000,000 on every
inked page and 0 on a blank one. Both are true statements and neither is
informative on its own. The field this record exists to carry forward is
`total_ink_pixels`: it is the pre-proposal denominator Unit 14's coverage
derives from. The alarm on this record is the *outcome* —
`unclaimed-edge-ink` — which is measured against a real central rectangle.

**A flagged edge record does not by itself make the run `partial`, but an
unreleased one now does.** `unclaimed-edge-ink` still classifies UNRESOLVED at
*this* boundary and terminates nothing here: this stage measures before any
proposal exists, so it cannot know whether an act claims that ink. What decides
the run is the Armarium's re-measure against the Designator's verified crops —
see the Unit 14B ledger below. `run_aggregate`
(`common/contracts/outcomes.py`) takes `edge_hold_pages` and appends a named
partial reason for every page whose edge ink no crop released, so a run
carrying one cannot report `complete`. A page whose ink the crops did claim is
released and adds no reason.

This paragraph previously recorded the opposite, as the deferral this unit had
chosen while Unit 14 was outstanding. Unit 14B has landed; the sentence is kept
here corrected rather than deleted because this file is the stage interface and
a consumer who built against the old contract needs to see that it moved.

## The fixture is a degenerate case for the edge detector

The synthetic pages are 200x260, so a 64-pixel perimeter leaves a central
rectangle of 72x132 — about 18% of the page. Both pinned scenarios therefore
flag **every** page `unclaimed-edge-ink`, and no run in the suite produces the
`mapped` outcome at all. On a real 300-DPI register page the same band is a few
per cent of the area, which is the bounded strip the constant is for.

Read a green fixture run accordingly: it proves the detector is wired, named
and bounded, and it proves nothing about the detector's selectivity, because
the fixture geometry does not leave a quiet perimeter for it to be quiet about.
`EDGE_BAND_PIXELS` stays where it is — moving it to make the fixture look
better would be tuning the instrument to the specimen, which is what
GOVERNANCE 10's second paragraph forbids. Selectivity is measured on real
material or not claimed.

## Unit 14B reconciliation ledger — release is by the same ink, not by exemption

The fixture's apparent degeneracy was measured against the actual declared
crop rectangles before changing it. On page 1 (200x260), the 64-pixel initial
edge band contains 8,328 of 11,520 ink pixels (72.2917%); the two declared act
crops leave 0 outside pixels. On page 2, it contains 3,384 of 3,840 pixels
(88.125%); its declared continuation crop likewise leaves 0. Thus the ink is
genuinely *claimed*; the semantic defect was treating a pre-proposal finding
as unreleased after the Designator had supplied coverage, not a specimen with
unclaimed edge ink.

Unit 14B therefore retains the fixture and leaves `EDGE_BAND_PIXELS` and
`MINIMUM_INK_PIXELS` unchanged. Armarium re-measures the Ink Map's retained,
lossless page-space runs against verified final Designator crop bounds. A clear
re-measure releases the page; a flagged re-measure holds it. The
`structure-failure` scenario is the positive: it cuts no page regions, so its
real fixture ink remains unclaimed, is held for review, appears in
`partial_reasons`, and refuses a complete export. This distinguishes a
pre-proposal signal from a genuine unresolved coverage finding without
weakening either.
