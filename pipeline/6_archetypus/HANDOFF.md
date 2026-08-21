# Archetypus — handoff

The Archetypus is the first and only current stage that calls one machine reading
established. It is not a correction, a witness consensus, or a truth claim. It writes
a once-only `kind="archetypus"` record under `6_archetypus/artifacts/` for an act whose
current Recensor review is exactly `accepted`. A held act deliberately has no Archetypus
record; that absence is part of the terminal accounting, not a gap to fill.

**Exit codes.** `EXIT_COMPLETE` when every accepted act has a record and the index
reconciles. `EXIT_HELD` when any act's current review is `recovery-requested`: that
outcome is unresolved rather than terminal, so the acts already established are real
but the stage's work is not finished, and the held act ids are named on stderr. A
refusal anywhere in establishment or index reconciliation is fatal, not a held act.

## Input boundary

The stage derives the current review by unique attempt ordinal. For an accepted review
it takes the digest-checked `perlectio_ref` carried by that review and resolves that
exact Perlectio — through `RunTree.read_artifact_reference`, which refuses a reference
whose actual bytes name a different stage or kind. That single check is what makes a
Testimonium, a hypothetical salvage-tier record, or any other non-Perlectio artifact
unable to reach this stage by being named in `perlectio_ref`. It does not independently
select whatever Perlectio is now latest. The Perlectio must be a completed-class reading
with valid serving provenance; a held Designator act may not be resurrected by an
accepted later review.

`accepted_primed_perlectio` closes the rest of spec 10's test 1 by name, so a producer
that later starts labelling its readings cannot slip bad material through on a field this
stage does not look at:

- an explicitly unprimed reading — `lectio_kind` naming anything but `primed`, or
  `primed: false` — is refused;
- `tier`, `source_tier` or `reading_tier` of `salvage` is refused (invariant #31's
  boundary);
- the reading must retain a non-empty Testimonium basis, and every entry's reference
  must be a direct sealed input of that reading and resolve as an
  `(attestatores, testimonium)` artifact for this act.

**A named assumption.** No Perlectio in this build records primed/unprimed at all; adding
that field is the Perlector lane's work. Until it exists, an *unlabeled* reading is
accepted and the retained Testimonium basis is the transitional indication that it was
primed. That is a compatibility assumption, not proof — the refusals above are real, the
positive claim "this reading was primed" rests on the producer eventually writing the
field.

## `kind="archetypus"`

The artifact subject is the stable act identity. Its payload is separately self-hashed,
its field set is **closed and checked** (`validate_record_fields`), and it contains:

```text
act_id, act_key, page_id
text, text_hash
status = "established", text_status
regions, provenance, annotations, uncertainty, evidence_ref
dissent_ref, perlectio_ref, recensor_ref, self_hash
```

The closed field set is the mechanical half of "exactly one `text` field": the old
pipeline's export reached through `consolidated_literal`, `reader_text`, `literal`,
`text`, `markdown` for whichever was non-empty, and a closed set is what stops that being
rebuilt one field at a time. A record missing a field, or carrying one it is not defined
to carry, is refused before it is written.

**`status` vs `text_status`.** `status` is a fixed literal, `"established"`, required
verbatim by the Armarium's own
`verify_established_record` — it means "this act has exactly one Archetypus record,"
record-level. `text_status` is the richer, separate claim spec 10 asks for:
`established | partial | no_readable_text`, describing what the record's `text` actually
contains. The two are deliberately different fields answering different questions. They
are **not** mirrors: mirroring them would make every damaged act fail the Armarium's
literal check, and would put a second status decision where there is meant to be one.

**`text_status` derivation.** Computed from the reading's own text and *both* damage
layers it carries, never stored upstream:

- any canonical `uncertainty.gaps` row, or any `illegible` annotation, present →
  `partial`, regardless of whether `text` is otherwise empty or full (ink is known and
  unread somewhere in the act);
- otherwise, empty or all-whitespace `text` → `no_readable_text`;
- otherwise → `established`.

The two layers are unioned rather than ranked, so neither can hide damage the other
saw — which is what makes carrying both of them honest rather than redundant. The gap
test precedes the empty-text test for both layers: a whole-act gap over empty text is
"ink present, wholly unread", the middle silence, and reporting it as the last one
would seal a proved blank beside a gap saying the opposite.

**The derivation lives in `common/contracts/outcomes.py`** (`TEXT_STATUSES`,
`derive_text_status`, `derive_record_text_status`), not in this file, because the
Armarium recomputes the same word from the layers travelling beside the text at
export and stages talk only through `common/`
(`pipeline/test_stage_import_boundaries.py`). One spelling; the two cannot drift into
disagreeing about the same record. This module re-exports all three under its own
names, which is how this stage's tests reach them.

An empty-text `established` record is refused at the schema (`validate_text_status`).
`no_readable_text` requires `evidence_ref` — see below. A blank page is **not** a fatal
error (Tyrel, 2026-08-05: "It is not a fatal error there might be blank pages"); what is
refused is the opposite collapse, ink that is merely unread reported as ink that was
never there.

**`evidence_ref` — an intentionally unfilled upstream contract.** An accepted review is
evidence that the Recensor accepted a reading; it is not evidence that the page was
blank. The constructor therefore accepts `no_readable_text` only when the review carries
a digest-checked `no_readable_text_evidence_ref` as one of its own direct inputs. The
reverse is fatal rather than ignored: a review that retains a blank proof over a reading
that establishes text is two upstream claims contradicting each other, and reading past
the one this stage does not need is how the contradiction would leave no trace. The
current Recensor publishes no such proof, and its `confirmed-blank` outcome is terminal
at that stage, bypassing this record. Consequently the current end-to-end pipeline cannot
yet publish a `no_readable_text` Archetypus. This is a named cross-stage gap, preferable
to manufacturing a blank finding from empty text; the shared outcome algebra likewise
keeps Perlector silence unresolved until a blank-proof contract exists.

`evidence_ref` is checked for shape and for membership in the review's own inputs, but —
unlike `perlectio_ref` and `recensor_ref` — it is never read, stage-checked, or
kind-checked, because no `blank-proof` artifact kind exists yet to check it against. The
one class checkable without that missing contract is refused: nothing from the reading's
own evidentiary chain may stand as evidence of its own silence — neither the reading
itself (`no_readable_text_evidence_ref == perlectio_ref`) nor any direct input of that
reading, including the crops it read. Both are a `SchemaRefusal`. Resolving the reference
through
`read_artifact_reference` against a real `kind="blank-proof"` — the way `perlectio_ref`
and `recensor_ref` are resolved — is still owed once the Recensor lane defines that kind.

**`annotations` — carried whole, never in `text`.** A list of:

- `uncertain` — `{kind, start, end, certainty, alternatives}`. A span covering at least
  one *readable* character in `text` — width alone is not enough, because a span over
  blank text would sit inside a `no_readable_text` record asserting the reader did read
  characters there. Where nothing was read, the honest shape is an `illegible` gap. It
  carries a closed certainty (`high | medium | low | unknown`)
  and the reader's own candidate readings for that span. The alternatives are the
  Perlector's, not a witness's: the Perlector reads the ink, so its uncertainty about a
  span it did read is its own. There is no witness-reference field on this shape at all.
  Be aware of what is *not* verified: alternatives are the one free-text field in the
  sealed record — non-empty, distinct strings and nothing more. Unlike a gap's quoted
  variant (checked verbatim against what its cited witness reported), nothing ties an
  alternative to the ink or to anyone; the claim that they are the reader's own rests on
  the producer, not on a check. `text` is unaffected either way.
- `illegible` — `{kind, start, end, witness_evidence}`. A **zero-width anchor**
  (`start == end`, structurally, so a gap can never carry a character), representable at
  leading, internal, trailing and whole-act positions. `witness_evidence` may be empty —
  every witness may have found the same damage — and each entry is exactly
  `{witness_ref, variant}`: the witness must be one of this reading's own basis
  testimonia, and **the quoted variant must be a substring of what that witness actually
  reported**. A variant that is neither the ink nor something a witness said is a
  reconstruction, and the record carries none (Tyrel, 2026-08-05: "we don't want it
  making shit up"). The comparison is exact; normalizing here would be a place for the
  record to differ from the testimony it quotes.

The shapes map onto the mature convention rather than inventing markup: `<unclear
cert="">` and `<gap>` (TEI P5, "Representation of Primary Sources"; EpiDoc Guidelines).
Rendering either of them — brackets, underdots, sigla — is the Armarium's business at
export time and is deliberately not stored here. `annotations` is optional on the wire
today: nothing upstream of this stage populates it yet, and it defaults to `[]`, which is
exactly today's behaviour.

**Beside, not instead of, the canonical `uncertainty` layer.** The two describe the same
kinds of damage — `uncertain` against `uncertain_spans`, `illegible` against `gaps` — and
each carries a fact the other's schema cannot hold: a `certainty` of `unknown` has no
canonical equivalent, and a canonical gap's `position`, `chair` and `testimonium_id` have
no place on an `illegible` note. Folding one into the other would therefore lose evidence,
which GOVERNANCE 4 does not allow, so both are sealed and both travel. They cannot
contradict each other into silence because `text_status` is the union of the two: either
one recording unread ink makes the record `partial`.

**A malformed annotation is run-fatal, with no per-act route around it — a producer
obligation, not (only) a reader problem.** `validate_annotations` raises `SchemaRefusal`
out of `main` for the whole run on the first malformed note it meets: a closed kind, exact
integer offsets within `[0, len(text)]`, a closed `certainty` enum, non-empty
`alternatives`, and — for `witness_evidence` — an **exact, unnormalized substring match**
against what the cited witness reported. That comparison is correct for what this stage
stores (normalizing here is where a record starts to differ from the testimony it quotes),
but once the Perlector lane starts emitting annotations from a language model, several of
these become reachable on ordinary model variance rather than only on forged input: a
witness quotation returned in a different Unicode normalization form (NFC vs. NFD) or with
a stripped trailing space is byte-different and refused; offsets are model-computed, so a
model counting grapheme clusters or UTF-16 units instead of Python code points is off by
one on every accented character; `certainty` values like `"very low"` or `0.7` are refused
outright. **The obligation this places on the Perlector lane: validate or repair a
reading's offsets and quotations against these same rules before sealing a Perlectio**,
so a malformed model output is refused (or normalized) at its own stage rather than taking
the whole Archetypus run down for every other act. Whether the comparison itself should
Unicode-normalize before checking substring containment (while continuing to *store* exact
bytes either way) is a product decision, not made here.

`text`, `regions`, and provenance are exact copies of the one reviewed Perlectio;
`dissent_ref` names that Perlectio artifact rather than making a second mutable dissent
copy. **`dissent_ref` and `perlectio_ref` are the same value by design, not by
accident**: `perlectio_ref` is the parent evidence this record establishes from,
`dissent_ref` is where a reader finds this act's dissent (Tyrel's 4d — by reference,
never copied); the dissent lives inside the Perlectio itself, so the two pointers
coincide. The Armarium's own frozen verification requires them equal, so carrying only
one under two names is not available without breaking that consumer. `perlectio_ref` and
`recensor_ref` are typed, digest-checked references and both are direct inputs, together
with the exact crop blobs named by the reading. `text_hash` is the canonical digest of
`text` alone (`digest_of(text)`), so any export format can prove it carries the same
clean text without re-hashing the whole record. One trap for a second implementer:
`digest_of` hashes the *canonical JSON encoding* of the string — `sha256('"Maria"')`,
quotes included — not the raw UTF-8 bytes. Computing "the sha256 of the text" the
obvious way produces a mismatch against every record.

There is no alternate text, no witness text field, and no branch that chooses among
readings. `establish_from_accepted_primed_perlectio` is the only public constructor: its
caller supplies an act and the sealed Recensor-review reference, never free-standing
text or a reading payload. A later run cannot write a second different record under the
same once-only identity.

## `6_archetypus/index.json`

A rebuildable per-run summary, derived from the immutable per-act records on disk exactly
as `manifest.json` is — never the only evidence. Written through `RunTree.write_index`,
so it is rewritable (unlike an artifact) and safe to delete and rebuild identically.

Each row is `{act_id, act_key, artifact_id, text_status, text_hash, relative_path,
sha256}`, and the index itself is the closed set `{schema, run_id, stage, record_count,
rows, self_hash}` — `record_count` counts the records the index summarizes; the
reconciliation below, not the field, is what ties that number to the Recensor's accepted
set. The stage rebuilds it, reads it back, and reconciles it before finishing:
rows, records on disk, and **the acts the Recensor accepted** must be the same set, and a
missing or duplicate row is FATAL rather than a warning. The reconciliation target is
deliberately the Recensor's accepted set, recomputed from the immutable review records —
an index checked only against the writer's own list would agree with itself about an act
the writer had skipped. `validate_index` proves the same thing for any consumer that
wants it before relying on the file — with one practical caveat: its first argument is a
stage-context-shaped object (`.tree`, `.fixture`, `.input_ref`, `.artifact_ref`), so a
consumer outside a stage builds a small shim first, exactly as `test_index.py` does.

## Consumer obligations

Armarium requires exactly one Archetypus record for an accepted act, rather than
selecting one. Before export it verifies the nested self-hash, both parent references and
direct-input chains, and exact equality of `text`, `regions`, `provenance`, `status`, and
`dissent_ref` with the reviewed Perlectio. It then links each region back to the original
Exemplar filename ledger.

**It also reads `text_status` and `annotations`, and does not take either on trust.**
`verify_established_record` validates and NORMALIZES both annotation layers through
the shared `validate_annotations` before comparing them — the sealed copy is
normalized (an `illegible` note always carries `witness_evidence`, defaulted to
`[]`) while the reading's raw copy may legally omit the field, so raw equality
would refuse a correct record; the validated forms must be identical. It then
*recomputes* `text_status` from that layer and the canonical `uncertainty`
beside it, using the shared `derive_record_text_status`. A record claiming
`established` over its own recorded gap is fatal at export. Both fields then travel:
into the manifest entry, the projection, every selected literal format, the package's
text-free source graph, and the run aggregate, where a non-`established` status
contributes its own named reason and the run reports `partial`. A run whose acts are
all delivered but damaged therefore exits `EXIT_HELD` at the Armarium rather than 0 —
the act is delivered, and the run did not read all of it.

Consequences worth stating plainly:

- the older `annotations` layer is **carried, not migrated**. It is projected under the
  name `transcription_annotations`, to keep it apart from the unbuilt *semantic*
  annotation layer (`pipeline/7_armarium/annotation_boundary.py`), whose per-row
  `not-produced` claim used to be written over it under the bare name `annotations`.
  Nothing upstream populates this layer yet, so `[]` remains the ordinary value;
- `evidence_ref`, `text_hash` and `index.json` are still not read at export. They
  remain fields this stage carries for their own sake; and
- the projection-identity test (`pipeline/orchestrator/test_projection_identity.py`)
  checks the one export format that exists. A second format must be added to it, or it
  will pass over the new one in silence.
