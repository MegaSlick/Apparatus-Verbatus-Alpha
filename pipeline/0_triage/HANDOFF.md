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

Work at the untrusted contract edge is bounded before it amplifies: a shard holds
at most 1,000 manifest rows and accepts at most 1,000 supplied cluster records, one
re-shoot cluster holds at most 4,096 member digests, and one frame holds at most 64
physical split parts. The last bound caps the split validator's pairwise overlap
proof; exceeding any bound is an explicit refusal, never a partial manifest.

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

**Unit 7 still owes half of one check this module cannot make.**
`source_frame_sha256` is opaque bytes-identity: nothing here can tell the digest of a
source frame from the digest of a ScanTailor crop output, and ScanTailor's output
images are never submitted. The cheap structural guard is the row's own `frame`,
compared against the decoded submitted image's dimensions, which catches a row bound
to a derivative rather than to its master.

Two things now stand where nothing did. `common.imaging.render_triage_derivative`
refuses a region that is not contained in the master it actually decoded, and
`verify_triage_derivative` refuses a sealed derivative whose row declares a frame of
a different size than the decoded master. Between them a row claiming a frame
*larger* than its master is refused, at render time and again at the Exemplar
boundary.

What remains is the other direction, at the door. A row declaring a frame *smaller*
than the submitted master still passes the door: its parts partition the small
declared frame and every one of them lies inside the larger master, so containment
has nothing to object to. Only the Exemplar boundary's equality check catches it,
after the page is sealed. Closing it at the door means giving the render path the
row's `frame` — the part alone does not carry it — which is a plumbing change Unit 6
should make deliberately rather than a guard that can be dropped into the renderer.

`triage-re-shoot-cluster-v1` is a corpus-scoped leaf record keyed by its member
**frame source digests**. It has no run id, no shard id, and no cluster digest
identity, so a cluster's members may lie in different corpus-frame shards and
therefore different manifests. A row may name a cluster only when its source digest
is a member, and all members declare the same split count. The mapping key a caller
files a record under must equal the record's own `cluster_id`, and the record's
`corpus_id` must match the manifest and every row it contains. Every frame remains
processable.

## ScanTailor seam — unverified

**UNVERIFIED FORMAT GAP — DO NOT TREAT THIS AS A REAL SCANTAILOR IMPORTER.**
`transcribe_scantailor_project` accepts only the checked-in XML fixture shape
`scantailor-project shape="unverified-fixture-v0"`. It is a defended parsing seam,
bounded to 4 MiB before XML parsing, not a claim about ScanTailor Advanced's real
project-file format. No real project file was available offline, so closing this gap
here was impossible under the inherited Q2 ruling. Every decision field — operation
order, coordinate spaces, each part's region, local crop, rotation direction, origin,
canvas rule, colour mode and deskew angle — is read from the fixture's own attributes;
none is synthesized or defaulted. Empty projects are refused. If real projects do not
carry per-page geometry — including split geometry specifically — Unit 6 must change
the transcription source explicitly; it must not synthesize geometry.

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
