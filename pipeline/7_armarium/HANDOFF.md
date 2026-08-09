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
  source digest, retaining the literal `canonical_clean_text` value, and beside it a
  `display:` rendering under the **proposed** convention named on the line above it.
  The rendering never replaces the canonical field: the clean verifier strips it and
  requires the canonical value back exactly. Tyrel has not chosen a convention, and
  `claims.display.status` says so on the face of every bundle.
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

### The terminal ledger

`claims.terminal_ledger` is the honesty ledger's total partition: every submitted
source page or frame, every sealed page, and every proposed act lands in exactly one
of the five closed categories, and a unit in none of them — or in two — stops the
export. The three populations overlap on purpose, so `by_unit_type` is published
beside `by_category`: an act, the page it was cut from, and the source that sealed
that page are three units describing one piece of material.

A source unit inherits the category of the page it sealed into, and a refused source
is `refused-with-reason` with the door's own reason. A sealed page is `delivered` when
any act on it was delivered, `excluded-with-approval` or `confirmed-blank` only when
every act on it was, and `held-for-review` otherwise — including when no act was
marked out on it at all, because silence cannot tell a blank page from a detection
failure and nothing here can prove one blank.

**What the denominator does not cover, said rather than implied.** `run.json` binds one
ordinal per submitted source *page or frame*, so a multi-page PDF or TIFF container is
represented by one unit per page and not by one unit for the file. Every submitted file
is therefore represented, but a reader counting files off this ledger would be counting
pages. `claims.submission_inventory.limit` says exactly that.

`claims.status` is the ledger's own measured status, not a constant: a run that loses
nothing says `complete`, and every unresolved unit appears by name in
`claims.partial_reasons`. The clean verifier recomputes the whole ledger from the
package's `sources.json` rather than reading it out of the manifest — a self-hash
proves the manifest was not edited afterwards, never that what it says was true.

Non-pixel references to receipts, Testimonia, and intermediate artifacts are
labelled `requires-retained-run-access`; the product carries their paths and
digests, not an invented claim that it contains the separate evidence package.
If no sealed salvage inventory reaches this boundary, the salvage format is present
but the manifest says `not-produced-no-sealed-salvage-inventory`, rather than
claiming a measured zero.

The annotation boundary in `annotation_boundary.py` is not wired into this
stage, configuration, or orchestrator, and is built only as the contract a future
`annotator` chair would occupy — spec 11 gates the build itself on Tyrel approving
the ARCHITECTURE wording that gives the layer its home. It carries the five fields
spec 11 names (`act_type`, `date` with a normalized form, `person` spans with roles,
`kinship` edges, flags), each drawn from a closed vocabulary fixed in that file, so
no free-form string can carry a second transcription out of it. Every annotation
must anchor to a real span of the established text, and one that does not is refused
at the schema.

**What that refusal cannot yet do is be *recorded*.** Spec 11 test 7 asks for a
hallucinated person to be "refused at the schema and recorded"; the recording half
belongs in the terminal ledger's `refused-with-reason` set, and the ledger has no
annotation unit type because nothing in this repository produces an annotation to
account for. The refusal exists and is tested; the accounting for it does not.

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
