# Host-only measured pass

This chamber cannot read the Montebello masters or call vision seats. On the host, use
the authority path `/Users/tyrel/Metis_Research/Parish Master Copies/Montebello (Notre-Dame-de-Bonsecours)/Montebello/`
and only `005469606_00062.jpeg` through `005469606_00068.jpeg`. Write proxies and all
real-material artifacts under `private/triage/measured/2026-08-22_005469606_62-68/`,
never under this repository's tracked paths.

Run three independent read-only vision seats (Sonnet, Opus, Fable) with the identical
prompt below. Record their resolved identities, revisions, master/proxy SHA-256s, and the
disclosure artifact required by `config/data_handling_policy.json` before any external API
receives an image. Store verdict files as `seat-*.json`, then run:

```sh
python -c 'from operations.triage.reconcile import reconcile_files; from pathlib import Path; root = Path("private/triage/measured/2026-08-22_005469606_62-68"); paths = sorted(root.glob("seat-*.json")); assert len(paths) == 3, f"expected exactly three independent seat files, found {len(paths)}"; reconcile_files(paths, root / "expected.json", root / "disagreements.json")'
```

Copy only the resulting structural JSON (never proxies, masters, act names, or ink text)
to `operations/triage/measured/2026-08-22_005469606_62-68/` when the host's data policy
allows it. State the unresolved-disagreement count; do not claim it is zero unless the
actual reconciled file says so.

Before reconciliation, validate the files without writing output. This catches a malformed
seat response while the seat context is still available:

```sh
python -c 'import json; from pathlib import Path; from operations.triage.reconcile import validate_verdict; root = Path("private/triage/measured/2026-08-22_005469606_62-68"); paths = sorted(root.glob("seat-*.json")); assert len(paths) == 3, f"expected exactly three independent seat files, found {len(paths)}"; [validate_verdict(json.loads(path.read_text(encoding="utf-8"))) for path in paths]'
```

## Seat prompt

Use the literal prompt below from this file. Frame-relative positions are per-mille integers
from 0 through 1000, never decimal fractions. Page count is an exact categorical string, not
a per-mille quantity. Loose-document rotation is an exact signed integer string in
millidegrees (`+12000`, `-3500`, or `0`), not a box coordinate and not governed by numeric
tolerance. `validate_verdict` refuses floats and boxes outside the frame, so a decimal answer
costs a second disclosure-bearing round of external calls.
`loose_document_face` is a closed enum: `written-side-up`, `written-side-down`, `indeterminate`, `none` — the first pass answered in open dialects and paid with a vocabulary-induced disagreement.

> Independently inspect the seven supplied review-scale proxies, each identified to you by
> its review-proxy SHA-256 and source filename. Do not consult another
> reader's response, and do not infer a preferred capture, winner, canonical page, or
> expected relationship between frames. For each proxy report: how many pages it shows,
> and where their boundary falls (`boundary_x_per_mille`); whether a loose document is
> present, and if so its side, its signed rotation in millidegrees, and its face as exactly one of `written-side-up`, `written-side-down`, or `indeterminate`;
> every act you can see, numbered contiguously as `act-001`, `act-002`, and so on in
> top-to-bottom order (break equal-top ties left-to-right; never use an act name or ink
> text), and for each act you can localize, its bounding
> box as `{x0, y0, x1, y1}` per-mille; which other supplied filenames, if any, show the
> same physical opening; and anything on the proxy you cannot account for. Enumerate an
> act even when you cannot localize it — the enumeration is the coverage denominator and
> a box is optional against it. Return one closed `triage-structural-verdict.v1` object
> with your resolved identity and revision, your `numeric_tolerance`, and your
> `box_tolerance_permille`.

Return exactly this JSON shape, with one `facts` entry for every supplied review proxy. Use
`review-proxy-sha256:<lowercase digest>` as the fact key. Keep all categorical keys present;
use `none` when no loose document or unaccountable structural material is seen. Add one
`same_opening_<full source filename>` key with value `yes` or `no` for *each other* supplied
frame, so absence cannot be mistaken for disagreement. Keep `acts` in that declared
contiguous order and use only those same opaque act identifiers as `boxes` keys. When a
proxy shows only one page, omit
`boundary_x_per_mille` from `numeric` rather than inventing a boundary.

```json
{
  "schema": "triage-structural-verdict.v1",
  "seat": {"identity": "RESOLVED_MODEL_IDENTITY", "revision": "RESOLVED_REVISION"},
  "numeric_tolerance": 25,
  "box_tolerance_permille": 30,
  "facts": {
    "review-proxy-sha256:LOWERCASE_SHA256": {
      "categorical": {
        "source_filename": "005469606_00062.jpeg",
        "page_count": "2",
        "loose_document": "no",
        "loose_document_side": "none",
        "loose_document_rotation_millidegrees": "0",
        "loose_document_face": "none",
        "same_opening_005469606_00063.jpeg": "no",
        "unaccountable_structural_material": "none"
      },
      "numeric": {"boundary_x_per_mille": 503},
      "acts": ["act-001", "act-002"],
      "boxes": {"act-001": {"x0": 40, "y0": 80, "x1": 460, "y1": 310}}
    }
  }
}
```

The two tolerances are declared by the seat and the reconciler takes the smallest across
seats: `numeric_tolerance` governs only frame-relative numeric observations such as
`boundary_x_per_mille`; `box_tolerance_permille` governs box coordinates. Exact counts,
signed rotations, sides, faces, and frame relationships are categorical and therefore require
unanimity. Neither tolerance is calibrated — state it as declared, not measured.

## Pass record — 2026-08-22, frames 005469606_00062–00068

Three independent vision seats (claude-sonnet-5, claude-opus-5[1m], claude-fable-5)
each read the seven review-scale proxies blind, under the identical prompt, with the
disclosure record written first (real-material artifacts, seat verdicts, proxies and
the disclosure record live in the local private/ store; only this structural summary
and the reconciled JSON are tracked).

## What reconciled (expected.json)

- **Cluster structure, unanimous:** 63/64/65 show one physical opening; 66/67/68
  show another; 62 stands alone. Loose documents present on all six clustered
  frames (right side on 63/64/66/67/68; left on 65), absent on 62.
- Act coverage denominators (union rule): 62→6, 63→6, 64→6, 65→5, 66→8, 67→8, 68→8.
- Page count 2 on 62/65/66/67/68 (63/64 disagreed — one seat read the right leaf
  as fully covered and counted 1).

## What did not reconcile (disagreements.json — 7 fact-groups, stated as observed)

Boundary positions and every act box fell outside the declared tolerances (25 / 30
per-mille); `loose_document_face` and rotation disagreed largely by VOCABULARY —
the enum was not closed at pass time (fixed for future passes: written-side-up /
written-side-down / indeterminate / none). Nothing here was resolved by preference.
This first pass also predates the reconciler correction that records differing act lists as
their own disagreement and the now-closed positional numbering rule. The private seat files
remain the evidence needed to replay that correction; the tracked union denominators must
not be read as a claim that the seats enumerated the same acts. Its tracked outputs remain
historical v1 documents; current v2 outputs also bind both halves to the same validated
verdict set with `verdicts_sha256` and to the same complete derived pair with
`reconciliation_sha256`.

## Consequence for the plan

The reshoots note (and Unit 28's trial composition) treated 66 as its own single
insert frame with 67/68 as clean neighbours. Three seats unanimously read 66/67/68
as ONE opening re-shot three times. The seven-frame sample therefore holds TWO
three-frame re-shoot clusters — a richer trial fixture than planned, and the
producer's confirmation path must expect both.
