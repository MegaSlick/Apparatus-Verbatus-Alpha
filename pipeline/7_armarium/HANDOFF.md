# Armarium — handoff

The Armarium publishes the terminal `kind="export"` record and one
`kind="manifest-entry"` per expected act. Both are ordinary artifacts under
`7_armarium/artifacts/`; the stage manifest is derived inventory, never a competing
output file. The `export` record's `bundle.reference` is a digest-checked input
reference to the content-addressed Armarium ZIP blob. That ZIP is the product
which leaves the pipeline; the internal artifact is only its accounting record.

## Export contract

The export payload contains the aggregate result, the expected-act count, delivered
and review entries, witness coverage, `pages`, and the bundle reference. Every `pages` row is one submitted
source ordinal and retains:

```text
ordinal
declared_path
declared_sha256
declared_bytes              when the filename ledger recorded it
ledger_sha256               for a real submission
container_page_index        for a fanned container page or animation frame
outcome and reason
```

This is the final citation link: an output can be matched to the original filename
and source digest, and a PDF/TIFF/animation page can be matched to its zero-based
source page/frame without guessing from the pipeline ordinal.

Each delivered entry's `source_regions` repeats that link for the exact crop used
by its text. A source-region row carries `source_page_ordinal`,
`source_page_id`, `declared_path`, `declared_sha256`, and any applicable byte
count, ledger hash, and `container_page_index`, alongside the crop digest. A
continuation therefore names both original pages it used rather than relying on a
reader to search intermediate artifacts.

## Product bundle

`EXPORT_MANIFEST.json` is the first ZIP member and self-hashes its own contents.
It inventories every other member by digest, names the exact `canonical_clean_text`
field and its UTF-8 SHA-256 identity, and reports the selected `formats.toml`
projection configuration. The bundle may contain these plainly specified formats:

- `text/_source_folder/<source-folder>/readings.txt` (or
  `text/_source_root/readings.txt` for the source root) — readable sections with a source page and
  source digest, retaining the literal `canonical_clean_text` value. It makes no
  uncertainty/gap display choice pending Tyrel's decision.
- `acts.sqlite` — an `acts` table with the literal Archetypus field, and a
  separate `act_search` / FTS5 layer whose search fold is visibly derived and
  revision-marked.
- `acts.jsonl` — one record per expected act, with canonical text only for a
  delivered act, provenance, source regions, and explicit unavailable/pending
  annotation fields.
- `review-items.jsonl` — held and refused act records with reasons and
  digest-checked evidence references.
- `salvage/items.jsonl` — a structurally separate salvage namespace. It has no
  act identifiers or canonical-text fields; promotion requires recorded approval
  and pipeline re-entry, never an export-time act.
- `sources.json` — cited source-page/frame rows with filename and digest, plus
  text-free per-act citation/outcome records and the non-text accounting basis.
  The clean verifier uses these to require every selected projection to retain
  the exact delivered provenance, every continuation region, and every held or
  refused reason; it does not treat a merely nonempty replacement as equivalent.

If `embed_pixels = true`, verified page and crop bytes are included beneath
`pixels/` and clean-machine verification opens them. If it is false, source and
crop references remain valid and digest-named, but the manifest says plainly that
pixel resolution requires retained-source access.

The current run authority binds source-page/frame rows, not a type-aware submission
file inventory with a terminal category per file. The manifest therefore reports a
reconciled five-category **act** partition and a separate page census, but marks the
bundle `partial` and the submission denominator `unreconciled` rather than claiming
the spec's requested file-level closure.

Non-pixel references to receipts, Testimonia, and intermediate artifacts are
labelled `requires-retained-run-access`; the product carries their paths and
digests, not an invented claim that it contains the separate evidence package.
If no sealed salvage inventory reaches this boundary, the salvage format is present
but the manifest says `not-produced-no-sealed-salvage-inventory`, rather than
claiming a measured zero.

The annotation boundary in `common/annotation_boundary.py` is not wired into this
stage, configuration, or orchestrator. It is a future read-only contract only.

## Boundary checks

Before the Armarium publishes any artifact, it reconciles every `run.json`
source-manifest ordinal to exactly one Exemplar page outcome. It independently reads
the one Exemplar `corpus-seal`, verifies its self-hash, page census, and input
references, then compares each row against the source manifest and page artifact.
For every sealed page it also rechecks the Door admission and content-addressed
pixel blob before export. A missing, duplicate, altered, or unaccounted page is
fatal; an Exemplar-refused page remains explicit evidence and contributes to a
visibly partial export rather than disappearing from the page set.

The act-level proposal seal remains the authority for expected acts. The Armarium
places each one in exactly one terminal category and retains a review reason where a
text cannot be delivered. An accepted act must have exactly one Archetypus record;
a non-accepted terminal act must have none, so the stage never selects one record
from an ambiguous or orphaned set. The Armarium does not choose among witness
readings or put witness text in output.

**Every sealed page must have had an act marked out on it, and that is checked per
page rather than per run.** The stage derives each act's page coverage from the
Designator regions actually cut -- not from the proposal seal's primary
`page_ordinal`, because an act running over a page break is cut on both sides and
examines both -- and hands it to the run aggregate, which names any sealed page no
act reached. Silence is not `confirmed-blank` evidence, and a check that asked only
whether the *run* produced any acts let every busy page discharge a silent page's
proof obligation. Nothing here diagnoses a blank page; that is the Recensor's, and
what artifact will eventually prove a page-level `confirmed-blank` is open -- the
category algebra is act-oriented and has no way to say "this page was examined and
held nothing".
