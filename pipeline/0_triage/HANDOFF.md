# Triage — handoff

`manifest.py` defines the pre-door `triage-decision-manifest-v1` contract. A
manifest names one non-blank `corpus_id`, and each submitted frame has exactly one
closed row bound to that corpus: source-frame SHA-256, full-frame dimensions, a
partitioning split, nullable re-shoot cluster id, closed confidence ordinal 0–4,
pipeline-wide mode (`manual`, `semi`, `auto`), resolved actor
`{kind, identity, revision}`, human-override flag, and derived
`manifest_row_sha256`. There is intentionally no winner, canonical, or equivalent
selection field.

## Geometry is per split part

Each entry of `split.parts` is a closed
`{region, crop_box, rotation, colour_mode}` record. `region` explicitly declares
`space: "frame"`; it is a half-open integer pixel rectangle, and all regions are
pairwise disjoint and cover the frame exactly, so nothing on a frame is dropped or
double-counted. `crop_box` explicitly declares `space: "part"`; its origin is the
top-left of the cut region, not the source frame. This makes the second half of a
spread just as unambiguous as the first: its local crop starts near zero rather than
repeating the frame's x offset.

`rotation` is the closed record
`{rotation_millidegrees, direction: "clockwise", origin: "crop-centre", canvas:
"expand"}`. `colour_mode` is per part, because the pages split from one frame can
legitimately require different conversion (for example, a colour insert beside a
bitonal register page). A frame-level colour mode could not express that without
discarding one page's intended treatment.

One frame-level crop and one frame-level rotation could not carry this unit's own
structural case — a document taped over the page at its own angle, where "no single
gutter exists for auto-split and no global deskew straightens both surfaces" — and a
bound spread's two pages each want their own crop besides. A page that was not split
declares one full-frame part explicitly.

**The order a consumer applies a part is data, not prose:** the split's closed
`operation_order` must be `region-crop-rotate`. Cut the frame-space `region` from the
untouched master, take the part-local `crop_box`, then rotate those cropped pixels
clockwise about their centre onto an expanded canvas. A consumer cannot instead
deskew and then crop, rotate counterclockwise, rotate about the frame origin, or clip
to the input canvas while still passing schema validation.

This manifest settles geometry, not raster implementation. Unit 7's required sealed
apply recipe still owns sampling, fill, conversion implementation, encoder, and
library versions; a renderer must not invent those as defaults from this record.

## Modes and refusals

The modes are Unit 1's pipeline-wide vocabulary, named once as
`common.contracts.stages.TRIAGE_MODES`. The schema, the sealed
`config/triage_modes.toml` declaration, and `require_triage_modes`'s point-of-use
recheck all read that one constant; Unit 1's driver joins it rather than declaring a
fourth spelling.

Every refusal in this module is a `SchemaRefusal`, the type the pipeline's live
recorders catch (`pipeline/7_armarium/run.py`, `pipeline/3_attestatores/run.py`,
`common/exemplar_boundary.py`). A refusal raised as the bare `ContractError` base
would escape all of them.

## What a consumer must still do

Derivative pages must carry `derivative_page_backlink(row, part_index)`. The link
contains the corpus, source-frame digest, row digest, and exact part index; a row-only
link cannot distinguish the two pages produced by a spread. The future door calls
`verify_submitted_frame(row, bytes)` before applying any geometry, refusing a row
whose source digest differs from the submitted bytes.

`validate_manifest(manifest, clusters)` refuses a manifest whose rows name a cluster
when no cluster records are supplied. The `clusters` argument is optional only
because a manifest naming no cluster has nothing to resolve — cluster references are
never left silently unchecked.

A manifest refuses two rows for the same submitted frame, but that check sees one
shard at a time. Two shards each holding a row for the same frame with different
geometry is a contradiction no single-manifest validation can see; Unit 6 owns not
producing one, and a derivative's backlink names a row digest, so a consumer that
resolves a backlink against the wrong shard finds nothing rather than the wrong
geometry.

**Unit 7 owes one check this module cannot make.** `source_frame_sha256` is opaque
bytes-identity: nothing here can tell the digest of a source frame from the digest of
a ScanTailor crop output, and ScanTailor's output images are never submitted. The
cheap structural guard is the row's own `frame` — the door should compare the decoded
submitted image's dimensions against `frame.width`/`frame.height` and refuse a
mismatch, which catches a row bound to a derivative rather than to its master.

`triage-re-shoot-cluster-v1` is a corpus-scoped leaf record keyed by its member
**frame source digests**. It has no run id, no shard id, and no cluster digest
identity, but the consuming Door refuses a submitted shard when a named cluster reaches
outside that shard. The enforced reach is therefore bounded by submitted-shard geometry;
the producer must not represent a cross-shard cluster as ingestible. A row may name a
cluster only when its source digest is a member, and all members declare the same split
count. The mapping key a caller files a record under must equal the record's own
`cluster_id`, and the record's `corpus_id` must match the manifest and every row it
contains. Every frame remains processable.

## ScanTailor seam — unverified

**UNVERIFIED FORMAT GAP — DO NOT TREAT THIS AS A REAL SCANTAILOR IMPORTER.**
`transcribe_scantailor_project` accepts only the checked-in XML fixture shape
`scantailor-project shape="unverified-fixture-v0"`. It is a defended parsing seam,
not a claim about ScanTailor Advanced's real project-file format. No real project file
was available offline, so closing this gap here was impossible under the inherited Q2
ruling. Every decision field — operation order, coordinate spaces, each part's region,
local crop, rotation direction, origin, canvas rule, colour mode and deskew angle — is
read from the fixture's own attributes; none is synthesized or defaulted. Empty
projects are refused. If real
projects do not carry per-page geometry — including split geometry specifically —
Unit 6 must change the transcription source explicitly; it must not synthesize
geometry.

The seam builds the actor itself from the project's recorded `version`, and refuses a
project that records none. A caller supplies the known corpus, batch mode and override
flag, but no actor claim: a caller-supplied version would assert something about an
artifact nobody read (GOVERNANCE 6), and a caller-supplied `kind` would let a
transcribed row claim to be natively produced, which is exactly what "distinguishable
by actor alone" is for.

## Two fields that look redundant and are not

`actor` records what produced the proposal — a person working directly, a resolved
model identity and revision, or the ScanTailor version. `human_override` records
whether a person then changed it. They are deliberately orthogonal: collapsing them
would make `human_override` a restatement of `actor.kind == "human"`, and a human
correcting a ScanTailor crop would have to be recorded either as ScanTailor doing the
correcting or as a human whose identity the project file does not carry. The
overriding person's own identity lands in Unit 4's decision-record shape, which is
where the plan puts a manual crop.

A human actor's `revision` is `null`, not a placeholder string. GOVERNANCE 6 binds the
resolved revision of the *model* that produced a record; a person has none, and a
required string would only buy a value that protects nothing.

## Unit 6B producer and confirmation contract

Unit 6A supplies only an offline co-visibility instrument. It calls no model, writes no
manifest or cluster, and asserts no link. Unit 6B is the sole path that may turn a reviewed
confirmation into `re_shoot_cluster_id`; an instrument verdict never does so directly.
Every confirmed cluster must trace to candidate evidence by
`(instrument_config_sha256, sorted source-frame digest pair)`, and the confirmation must
retain the evidence, its pass manifest, and the human or fixture/measurement authority that
confirmed it. No designation means no register or manifest write.

For every producer pass, write the exact closed value returned by
`operations.triage.instrument.producer_recipe(load_config(...))` beside the decision
manifest and pass it to the Door as `--triage-producer-recipe`. The recipe binds proxy
scale and encoder, the 64x48 mean-plus-ink signature, comparison and selection parameters,
imaging-library versions, the exact bounded determinism claim, every tuning value as
`UNMEASURED`, and `known_blindness`. Its digest belongs only under the Door's
`triage_document_digests["triage-producer-recipe"]`; the producer configuration remains
outside `run_config_bindings` because it executes before a run exists. The run authority
still hashes triage document digests without recording them by name; Unit 6A deliberately
does not change that inherited Unit 5 shape, and 6B's pre-door confirmation does not depend
on learning them from a run.

Consume each complete `cluster-candidate-evidence.v1` record together with its
`cluster-candidate-evidence-manifest.v1` and matching recipe. A record's `near-duplicate`
verdict means only that two signatures agree; its record-local `near_duplicate_reason`
explicitly says it is not page identity, and its threshold snapshot declares
`measurement_status: UNMEASURED`. The recipe's `known_blindness` is load-bearing: different
blank or near-blank openings of one printed form, and different openings with co-located
ink, can be indistinguishable from a real re-shoot. Ink-count totals measure within-cell
contrast, not ink volume; uniform blank, dark, or blown cells all count zero. Confirmation
therefore reviews the frames, reason, blindness list, integer measures, and any disagreement;
it never promotes a verdict by itself.

One pass accepts each source-frame digest exactly once. Pair identity is the sorted pair of
distinct source digests, regardless of submission order. The evidence manifest closes over
exact pair multiplicities, names every unequal-dimension refusal by digest, and carries the
frame set and instrument configuration that produced it. A duplicate frame digest, a
duplicate emitted pair, a missing selected pair, or an evidence/recipe configuration mismatch
is a whole-pass refusal. `_refuse_preference` remains mandatory on the recipe, every evidence
record, the evidence manifest, confirmations, decision-manifest rows, cluster records, and
both corpus-register record types.

Submitted frame names are canonical relative POSIX paths. Traversal, non-canonical spellings,
and Unicode-normalized case variants are whole-pass refusals so the producer and the Door
cannot sort or key one submission differently on default APFS. Candidate records and named
dimension refusals are count-checked against the evidence manifest before either list is
walked, and an evidence manifest may name only frames in the producer submission. Pillow's
decompression-bomb warning and error are producer refusals, and the canonical confirmation
loader reads at most 16 MiB from a direct regular file without following its final path.

The two ScanTailor fixture seams must be replaced together when real transcription lands.
A real project has no trustworthy `source_frame_sha256` attribute: Unit 6B computes a
path-to-digest map from the submitted master bytes and binds each transcribed row through it.
Nor does a project supply this pipeline's confidence ordinal: every transcribed row records
confidence `0`, with `actor.kind == "scantailor"` preserving its origin. Do not synthesize
geometry the real project does not carry, and verify its rotation sign convention against a
real project before claiming transcription. A frame ScanTailor omitted still receives one
explicit full-frame producer row at confidence 0 so coverage remains exact.

Before producing rows, inspect every real master's decoded mode and dimensions. A mode outside
`common.imaging.ENCODER_LOSSLESS_MODES` requires an explicit per-part colour conversion; an
unequal-dimension candidate is recorded as refused, never dropped. The signature offset is in
cells on the reduced proxy and measures no master geometry, so it may never seed a split, crop,
or rotation. All three shipped triage modes continue to route every row to review until the
real measured pass establishes thresholds; synthetic fixtures are regression cases, never
calibration data.

### Closed confirmation-file and reconciliation contracts (Unit 6B)

The producer consumes canonical JSON `triage-re-shoot-confirmation.v1`, with exactly
`schema`, `corpus_id`, `appending_run`, `authority`, `instrument_config_sha256`,
`evidence_manifest_sha256`, and `clusters`. `appending_run` is the non-empty producer
pass identifier selected before a Door run exists. `authority` is a closed
`{kind, identity, revision}` record: `human` has null revision; the only non-human
authorities are the checked-in fixture or a measured pass and both name their revision.
The two digest fields retain the exact instrument configuration and candidate-evidence
manifest the confirmation reviewed.

Each confirmation cluster is exactly `{pages, evidence_pairs}`. A page is exactly
`{volume_id, designation, member_frame_sha256}` and names every submitted frame that
shows that physical page. `evidence_pairs` is a non-empty list of sorted, distinct
two-digest pairs drawn from the cluster's members. The producer derives each page's
`physical_page_id`, then derives `cluster_id` as `"rsc_" + digest_of(sorted(
physical_page_ids))`; membership growth cannot rename that id. A frame may be a member
of every physical page it shows. Missing designation, an unsubmitted member, overlapping
clusters, incompatible split counts, or a cluster span above the shard cap refuses the
whole producer pass. The register receives one ordered append — declarations before
membership links — and only then may the caller write the already-validated manifest
cluster records and their row ids. No confirmation means no cluster record and all
`re_shoot_cluster_id` fields remain null.

Every `evidence_pairs` entry must be a pair the instrument actually evidenced: the
caller supplies the exact producer recipe, evidence manifest, and per-pair
candidate-evidence records the confirming authority reviewed. The producer validates the
recipe, binds its instrument-configuration digest, checks every evidence record's threshold
snapshot against it, and consumes the manifest's candidate-selection conservation record:
the frame count and all-pairs prefilter denominator, the exact submission-window pairs, the
selected/refused reach, and every named unequal-dimension refusal must reconcile. This is
independent of `emitted_pairs_sha256`: a selector that silently drops a required window pair
cannot make its shortened evidence list verifiable merely by hashing that shorter list. The
producer also refuses when either confirmation digest field disagrees with the supplied
records or when a named pair is absent from them. A
confirmation's own `instrument_config_sha256`/`evidence_manifest_sha256` are otherwise
just well-formed strings; nothing else binds a confirmed link to real instrument
output. `commit_confirmed_production`'s retry is idempotent across the register/Door-
document boundary: a caller that re-reads the register's true current digest after a
crash between the register append and the Door-document writes converges on
republishing the same documents rather than being refused forever by a commit that can
never again find "new" membership to add. This is exact-state idempotence, not rollback:
a confirmation that omits any capture in the current membership head is refused before
Door documents can regress to the subset. A caller that never re-reads still gets the
ordinary concurrent-write refusal.

`operations.triage.reconcile` consumes only closed
`triage-structural-verdict.v1` files. It asserts categorical facts only when every
independent seat agrees. Numeric observations are retained as `[min, max]` intervals
only if their spread is within the smallest declared tolerance; it never computes a
mean. For every disagreement — including a fact only some seats reported at all — the act
coverage denominator is the sorted union of every seat's act enumeration, and a
missing-fact record retains both the reporting seats and each reported fact in full.
Different act enumerations remain a disagreement with every seat's list even while their
union stays in the coverage denominator. Act identifiers are contiguous positional names
(`act-001`, `act-002`, …) assigned in top-to-bottom reading order, breaking equal-top ties
left-to-right, so independent files do not silently give unrelated regions the same
arbitrary key. Consensus gates what the fixture asserts, never what counts as present. Act
geometry travels as `boxes`: per-mille integer
`{x0, y0, x1, y1}` rectangles keyed by an act the same seat enumerated, refused outside
0..1000 and refused as floats, reconciled per coordinate within the separately declared
`box_tolerance_permille`. An act nobody localized stays in the denominator without an
interval. It writes the two canonical, replayable documents
`triage-structural-expected.v2` and `triage-structural-disagreements.v2`. Both carry the
same `verdicts_sha256` over their ordered validated inputs and the same
`reconciliation_sha256` over the complete derived pair before that shared field is inserted.
A crash between the two separate pathname replacements therefore leaves a detectably mixed
pair even when two reconciler revisions process the same inputs. Detection means calling
`validate_reconciliation_pair`, which recomputes the complete-pair digest rather than merely
comparing the two carried strings. Verdict paths are direct regular files, not symlinks;
inputs are capped at 16 MiB each, 32 independent seats, 10,000 facts per seat, and 100,000
observations per seat. Structural numeric observations are frame-relative per-mille integers
from 0 through 1000. These deliberately generous ceilings bound an untrusted seat response
without constraining the seven-frame measured protocol. A host runs the actual
image-reading seats separately, never this producer.

### What a confirmation is authority for, and what binds it (Unit 6B audit)

A confirmation authorizes a corpus-lifetime write. Unit 0D's boundary — the pipeline
cannot approve itself — applies here in spirit, so it is worth saying exactly where the
line currently falls rather than implying a stronger one.

**The producer cannot manufacture one.** `operations.triage` has no model client and no
path from instrument output to a confirmation: `candidate_evidence` emits a recorded
verdict per pair and never a link, and `produce` mints a cluster only from a confirmation
handed to it. There is no code path in which running the instrument produces a
confirmation.

**What binds a confirmation to an operator act, pre-Unit 21, is that a person put the
file there.** That is the whole of it, and it is worth being blunt: `load_confirmation`
reads canonical JSON from a path, and `produce` accepts an already-parsed mapping, so an
in-process caller can synthesize one without any file existing. The `authority` record
(`{kind, identity, revision}`) is a *claim* the confirmation makes about itself, not a
credential anything verifies. Cryptographic trust roots for approval records are settled
permanently against (integrity-only records are the design), so this is not a gap waiting
on a signature scheme; it is the honest shape of a pre-console act.

Three things make that shape safe enough to ship, and each is enforced rather than
documented:

1. A confirmation cannot invent its evidence. It names an instrument configuration and an
   evidence manifest by digest, and the producer reconciles the supplied candidate-evidence
   records against that manifest's own accounting (`emitted_evidence_records`,
   `emitted_pairs_sha256`) — not merely against the configuration digest, which is public
   in the manifest and so can be quoted by an invented record. A pair the instrument
   refused to compare, or never selected, cannot be confirmed.
2. A confirmation cannot reach past its submission. Members must be submitted frames and
   must appear among the frames the instrument pass actually saw.
3. The confirmation itself is retained. `commit_confirmed_production` creates its
   `authority_path` immutably *before* the register append and either Door document, so a
   published cluster or membership can never be found without the authority that made it.
   A byte-identical retry may reuse that path; different bytes are refused and require a
   new path, so a later confirmation cannot overwrite the earlier act. If the register
   append is then refused, the authority remains as evidence of the attempted confirmation
   and makes no claim that the register changed.

**Unit 21 replaces the placement, not the schema.** A console act should supply the same
closed `triage-re-shoot-confirmation.v1` object with `authority.kind = "human"` and a
resolved operator identity, and should bind that identity into the register record rather
than only beside it. The record shapes in `common/corpus_register.py` are closed, so that
is a deliberate contract change for Unit 21 to make, not something to add quietly here.

### Correcting a confirmation that was wrong

The instrument is blind to exactly one thing that matters: two frames that agree
everywhere because neither carries ink. Two blank forms are a near-duplicate by every
proxy the signature grid computes, so a human can confirm them as one physical page and
be wrong. Memberships are append-only and grow-only, so this needs a recorded correction
rather than an edit, and both homes have one:

- **Register.** A `retraction` record may name the *current head* of a page's membership
  chain (`membership:<digest_of(link)>`), which restores the predecessor it grew from.
  Only the head, because every link contains its predecessor's members; withdrawing one
  from the middle would leave every successor asserting the captures it withdrew. A page
  corrected two links deep takes two retractions, and a page back to no link reads as the
  empty list. The retracted link and its reason stay in the register as evidence
  (GOVERNANCE 4), and `members_of` stops returning it (GOVERNANCE 2).
- **Door documents.** `manifest.json` and `clusters.json` are republished wholesale, not
  appended to, so the correction there is a producer pass without the wrong confirmation.
  Each confirmation has its own retained authority document; together with the register's
  `appending_run`, these tell a later reader which confirmation the withdrawn membership
  came from.

Every later confirmation append reads the register's *replayed* membership heads, never the
last historical membership record: a retracted link remains in history but is not current.
The optimistic register digest refuses a confirmation writer that raced a retraction before
it can republish Door documents. Replaying the exact withdrawn confirmation act is also
refused because it would repeat the same immutable membership identity; placing a fresh
confirmation with a new `appending_run` and authority path may reassert the same members
and republishes both homes together. Thus a retraction remains visible even while the
wholesale Door-document
correction is pending, and no confirmation retry can silently make the manifest assert a
membership that replay of the register does not.

### Cluster span is measured in Door ordinals

A confirmed cluster's span is checked at produce time against `max_pages_per_shard` in
the units the Door actually shards: sources ordered by relative path, one ordinal per
split part (`expand_sources`), with every seam between a cluster's first and last ordinal
blocked (`content_aware_shards`). Two taped-insert frames of five parts each are ten
ordinals, not two pages. Counting members in submission order would let this producer pass
a cluster no shard can legally hold, and a submission with no legal seam left is refused
whole at the Door — where nothing can any longer explain why. Two frames submitted at one
relative path are refused for the same reason: the Door has no distinct place for them.
