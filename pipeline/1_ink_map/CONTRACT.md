# Ink map — contract

The ink map runs after the Exemplar seal and before the Designator, and measures where
ink lies on each sealed page with no model involved. It writes one `kind="ink-map"`
record per sealed page, zero-ink pages included, measured by
`common.residual_ink.ink_map_page`: one background inference and one page-spanning
labelling per page, from which the edge finding and the retained runs are both taken.

```text
python pipeline/1_ink_map/run.py --run-root <dir> --run-id <id>
```

## Inputs and refusals

The stage opens only over an Exemplar that sealed its boundary. Before writing
anything it verifies every sealed page with
`common.exemplar_boundary.verify_sealed_page_pixels` and matches each to exactly one
`source_manifest` row by ordinal; a page it cannot bind, a duplicated ordinal or an
empty census refuses the whole stage with nothing written. Pages are then read and
measured one at a time.

The policy is `config/ink_map.toml`, proved against the run's `ink-map` seal before
use. A run whose `ingress` record is not a valid fixture-or-real record is refused.

## The paper value is shared, the contrast is this stage's

Every count is taken below the paper value from
`common.background.infer_background_evidence`, under the sealed `[background]` block
resolved for the page's own dimensions: the same inference, policy and bytes every
stage that reads paper uses. The raw histogram mode is not used, because on a
photographed opening it is the dark surround and would leave the page with no ink.

The contrast is not shared: `MINIMUM_CONTRAST_BELOW_BACKGROUND` is this stage's own 40,
a reasoned default not measured on real pages. Sharing the derived margin too would
make this measure a restatement of the scan it exists to check. Because 40 is below
the margin a photographed page derives for itself, this audit counts more ink than the
derived scan, some paper included.

`payload["background"]` records the paper value, the branch it came from, the page's
dark mode, its derived margin, this stage's contrast, the resulting ink threshold and
the policy digest, so a reader can recompute the level the page was measured at.
Consumers check the margin with `common.background.validate_measured_ink_map_payload`.

## Outcomes

- `mapped`: measured, no unclaimed edge ink.
- `unclaimed-edge-ink`: the perimeter strip holds ink above the sealed gates.
- `ink-not-measurable`: the shared inference refused the paper (majority ink, too
  dark, or a paper value that leaves most of the page as ink), or the paper is too dark
  for this stage's contrast of 40. The record carries `ink_measurable: false`, the
  refusal text and the policy digest, and **no** `edge` or `edge_findings` key, so a
  consumer fails loudly instead of reading zero as a measurement. The page stays in the
  census and is still read; the Armarium records it as `initial_outcome:
  "ink-not-measurable"` with `remeasured: null`.

Each record carries `payload["page_ordinal"]`, the identity a consumer joins on.

## The edge finding and retained runs

`payload["edge"]` measures only the page's perimeter strip: `[coverage_audit]
edge_band_bp` of the shorter side, with the area gate `substantial_ink_area_bp` of
the page's area and the noise floor and fraction gate under
`[coverage_audit.noise_floor]`. Every finding carries the resolved gates beside the
counts they decided.

The counts are the page's audited ink: `total_ink_pixels` excludes the one
page-spanning component (bounded by `[page_spanning]`), `page_ink_pixels` is every
pixel the audit calls ink, and `page_spanning_ink_pixels` is the part taken out.
`total_ink_pixels` is the denominator later coverage checks derive from.

`payload["edge_findings"]` retains the audited ink as lossless page-space runs
(`ink-runs.v2`), so the Recensor and the Armarium re-measure the same pixels;
`common.residual_ink.reconcile_edge_finding_with_runs` requires them to span the
sealed page. Their size on a full-resolution register page has not been measured.

An `unclaimed-edge-ink` record is unresolved evidence, not a hold: this stage runs
before any act is read. The Armarium re-measures the retained runs against the final
act regions under the same gates; a clear re-measure releases the page, a flagged one
holds it, and a run with a held page cannot report `complete`.

On the fixture's 200x260 pages the edge band resolves to 2 pixels and holds no ink, so
a fixture run maps every page `mapped`; the other two outcomes are covered by tests
over pages built for them, not by any committed run tree.

## Calibration

The `[background]` values were measured on 127 real pages, the `[page_spanning]` bound
on 17 and the coverage gates on 44; `[connectivity]` and the noise floor are reasoned
defaults. Each block's provenance in `config/ink_map.toml` states its sample and what
it does not establish, and `CALIBRATION.md` records how each number was chosen.
