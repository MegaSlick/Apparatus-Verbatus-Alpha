# Spec 05 — Perlector reading-claim measurement framework

Status: this is a durable instrument protocol written before this checkout has an
evaluation image, ground-truth transcription, model call, pod, or reading-quality
number. It is not a result. Tests exercise the single candidate interface with
synthetic fakes only.

The original disposable-spike framing has deliberately become a durable measurement
framework: the protocol, scorers, controls, gates, and public evidence shape survive
the spike. This document is the predeclaration that later bench and dress-rehearsal
work must use, not a retrospective explanation.

The declared-run entry point hashes this exact document and compares it with a
reviewable protocol pin in the code. A caller-supplied protocol digest is insufficient:
the sealed manifest and its run-plan approval must name this document's digest.

The question is: does a Perlector grounded in the **Exemplar** and informed by fallible **Testimonia** read better than its **Lectio nuda** and than every Testimonium alone? The image-absent control asks whether a candidate merely summarizes testimony. The framework measures; it never picks a candidate, witness, reading, or Perlector chair.

## 1. Preconditions before any material is shown

A real run is refused until the following are sealed privately: Tyrel's approval of the small, reasonable test, exact data, candidates/models, and budget (GOVERNANCE 9); a current data-gate approval for private-register material; an immutable selection manifest; an approved interim Attestator configuration; checked human references; and a prompt-format proof for every candidate.

The run boundary verifies three content-addressed Tyrel approval artifacts through the repository's existing approval-record contract: a `RunPlanApproval` binds the protocol, selected manifest, three-role candidate roster, sealed witness configuration, prompt-declaration snapshot, selected normalizer, budget-evidence digest, and private sample-accounting digest; a `NormalizationApproval` binds the selected profile; and, when private register images would leave the boundary, a `ThirdPartyTransmissionApproval` binds the vendor, resolved artifact, exact page set, and manifest. `DataGateAuthority` separately rechecks the current data-gate policy and its record. A SHA-shaped string or an in-memory `approver` field is not authority.

The immutable selection manifest records its frame digest, this protocol's digest, fixed seed, and predeclared stratum quotas. Its selected rows also bind the image provenance class, reference status, and non-literal SHA-256 values for checked reference, drafts, adjudication, raw Testimonium evidence, and both closed normalization forms. The declared-run entry point recomputes those bindings and refuses a caller-supplied material label, reference status, or text evidence that differs from the approved manifest. The code will not invent a quota or replace a short stratum. Each scoreable act needs two independent human transcriptions and an adjudicated checked-reference revision before any candidate or witness result can inform a prompt, crop, padding, or other adjustment.

Each candidate must supply resolved identity, revision/digest, exact declared prompt-format bytes, prompt-format digest, source-evidence digest, modality contract, and a byte-fidelity test. A placeholder vendor prompt is a refusal, not an approximation. The harness gives the adapter a length-prefixed delivery envelope containing the exact registered raw prompt bytes and exact common-dossier bytes, records its digest in the Perlectio, and refuses a receipt mismatch. It does not claim that an opaque vendor internally parses those bytes correctly; that residual limit remains private run evidence rather than a fabricated proof.

## 2. Candidates and one interface

Every candidate implements `Candidate.read(CandidateRequest)`. The request always carries the same semantic `Dossier`, plus that candidate's declared prompt-format bytes. The dossier gives each Testimonium a stable anonymous source slot, never its private Attestator identity, so a candidate cannot learn a witness-name preference. Every response becomes the same private `Perlectio` shape: resolved identity, condition, state, candidate text, dossier/prompt digests, image-presence, Testimonium count, structural dissent, wall time, and cost.

The required roster is: (1) `Qwen/Qwen3.5-9B` as the stock base settled by Tyrel; (2) one unaltered vendor vision model, named only in Tyrel's approved run plan; and (3) Tyrel's trained checkpoint from its own model repository by its own digest. The checkpoint has no special adapter, score, or prompt fallback. It is an ordinary candidate.

The framework rejects `Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR`: it is an Attestator, so making it a Perlector candidate would make the dissent evidence agree with its own witness. Every sealed Attestator source also records its private source ID, repository, revision, and artifact digest. The runner refuses any candidate that shares a witness source or artifact, even under a different private label. Names, repositories, revisions, and prompt bytes stay private. Public aggregation uses non-identifying integer slots only; a slot is not a ranking.

## 3. Common dossier and complete matrix

Every selected act runs for every candidate under all three conditions:

| Condition | Exact image | Testimonia | Purpose |
|---|---|---|---|
| `lectio_nuda` | present | absent | image-only baseline |
| `witness_primed` | present | every sealed Testimonium in sealed order | test whether testimony helps an ink reading |
| `image_absent_control` | absent by type and request field | the same sealed Testimonia | test witness-only summarization |

The runner constructs all dossiers and prompt requests before any candidate call. It refuses a missing prompt format, unscoreable reference, incomplete act, altered prompt-declaration snapshot, incomplete witness roster, unapproved run plan, or missing vendor approval before a partial matrix can exist. It never silently skips a cell. A response with a matching delivery receipt but missing/refused text remains a scored cell; an adapter error before delivery invalidates the measurement rather than becoming a model score.

Dissent is calculated only after candidate text is fixed. It counts comparable and departing Testimonia after the same normalization. It neither changes text nor weights, merges, selects, or rewards a witness.

## 4. Sampling frame, seed, and held-out rule

The frame is every Tyrel-approved manually cropped act with opaque act ID, source-page SHA-256, crop SHA-256, provenance class, predeclared century/record/damage stratum, and potential for a checked human reference. No criterion may depend on model output, inter-annotator agreement, cost, or apparent ease. The full pre-reference frame may be selected without text hashes; every selected member must then bind its reference status plus checked-reference, independent-draft, adjudication, and Testimonium hashes before a run can open. `no_readable_text` and `unresolved_gap` must have a private reason-evidence digest in `PrivateSampleAccounting`; that accounting must partition every selected act into scoreable or excluded, is itself in the run-plan approval, and cannot replace a selected act.

The seed is fixed now: `verbatus/spec05/selection-v1`. Within each sealed stratum, sort `SHA-256(UTF-8(seed) || 0x00 || UTF-8(opaque_act_id))`, then take the sealed quota. The quota is Tyrel's GOVERNANCE 9 judgement; the code refuses a missing, altered, or undersupplied quota. It records seed, algorithm, frame digest, selected-member digest, and protocol digest.

The manifest retains the full opaque frame and recomputes its own deterministic draw; a claimed selected list that differs is refused. The declared-run entry point binds every supplied act's opaque ID, source-page digest, crop digest, provenance class, reference status, and private-evidence hash set to exactly that selected list before any dossier or adapter call. The selected crop **and source-page** digests, checked-reference/draft/adjudication evidence, and raw/record/closed-normalization Testimonium hashes are locked by `HeldOutUseGuard` before prompt tuning, padding calibration, adjustment, later bench, or dress rehearsal. A selected payload must be bound to its opaque act before it is transformed; that lineage survives in-framework transforms and prohibited uses refuse regardless of the derived bytes. Bare bytes and bare digest lists refuse for those prohibited uses rather than pretending to prove disjointness. A later stage needs its own separately approved provenance for new material. Locking the page prevents a neighbouring crop from pretending to be disjoint. It mechanically protects callers that enter through this framework; it does not claim to police unrelated manual conduct outside it.

## 5. Ground truth and human adjudication

1. Two qualified people independently make diplomatic transcriptions from the Exemplar crop. They see neither model nor Testimonium output or one another's draft, and mark unread ink as a gap rather than guessing.
2. Preserve both raw drafts privately. Record agreement after the sealed normalizer, but never use agreement to select easy acts or form a majority reading.
3. A third qualified person adjudicates every disagreement against the Exemplar and records a character/span decision or `unresolved_gap`. No model or witness sets the reference.
4. Independent QA checks the reference revision, crop/page digest, and normalization profile digest. The resulting checked reference is immutable; later corrections create a new revision and invalidate comparability rather than overwrite evidence.

`no_readable_text` is a positive fact about a truly blank crop, never an empty string. `unresolved_gap` means ink exists but cannot be adjudicated. Neither has a CER/WER denominator, so neither gets an artificial perfect score. Both remain accounted for until a separately predeclared masked-alignment method exists.

## 6. Normalization: recommended `graphemic-v1`

Raw text is retained privately. Apply this sequence identically to reference, candidate, and Testimonium text before CER/WER:

1. Require Unicode text and NFC-normalize it.
2. Replace each Unicode whitespace run with one ASCII space; trim outer spaces.
3. Map U+2018, U+2019, and U+02BC to ASCII apostrophe; map U+2010/U+2011 to ASCII hyphen.
4. Expand only `ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ`; do not apply NFKC or NFKD.
5. In `graphemic-v1`, map long-s `ſ` to `s`.
6. Preserve case, all remaining punctuation, diacritics, digits, historic spelling, abbreviations, `i/j`, `u/v`, and `œ/æ`. Never case-fold, strip accents, modernize, remove punctuation, or correct a scribal error.

CER uses UAX #29 extended grapheme clusters (spaces count). WER uses nonempty runs between canonical spaces, leaving punctuation inside a token. `uniseg==0.10.1` segments clusters; no library normalizer is used. Each run records the profile digest. The conservative historic-spelling choice is supported by the [OCR-D transcription guidance](https://ocr-d.de/en/gt-guidelines/trans/transkription.html), and NFC/reference-length CER by [OCR-D's evaluation specification](https://ocr-d.de/en/spec/ocrd_eval.html).

### Required Tyrel decision before the first real run

Long-s/allographic handling can favour a candidate. The recommendation is `graphemic-v1`, treating long-s as an allograph while preserving historical spelling. The alternative `allographetic-v1` preserves `ſ`. Tyrel must select one named, hashed profile before the evaluation manifest opens. The same approval confirms that `i/j`, `u/v`, `œ/æ`, diacritics, and historic spelling stay significant. No hidden normalization knob may change after results appear.

## 7. Exact CER/WER and response states

For each scoreable act independently, align normalized reference and hypothesis with unit-cost Levenshtein operations. `rapidfuzz==3.14.5` is pinned and called with `processor=None` and `weights=(1,1,1)`:

```
CER_act = (insertions + deletions + substitutions) / reference grapheme clusters
WER_act = (insertions + deletions + substitutions) / reference word units
```

Only after per-act alignment, micro-aggregate each candidate/condition:

```
CER = sum(act edit counts) / sum(act reference clusters)
WER = sum(act edit counts) / sum(act reference words)
```

Rates can exceed 1.0 due to invented insertions. They are never clipped or divided by the longer string. Every Testimonium gets this exact direct baseline against the same checked reference; no silver or witness-derived reference is admitted.

| Observed response state | CER/WER hypothesis | Extra accounting |
|---|---|---|
| complete | supplied text, including an explicit empty string | complete count |
| truncated | supplied partial text | truncation count; missing tail deletes |
| refused, missing, unavailable, malformed | empty text | named state count; all reference units delete |
| unproved adapter delivery | none | invalidate the measurement; do not score a harness defect as a model failure |

A refusal cannot improve by disappearing. Synthetic hand-worked tests pin exact match, one substitution/deletion/insertion on `abc`, empty against `abc`, two-error WER, composed/decomposed acute, whitespace, and preservation of case/punctuation/diacritics.

## 8. Measures and evidence, never a picker

For each candidate slot × condition, report CER/WER numerator, denominator, and rate; character completeness; every state count; structural dissent counts/rate; observed wall-time mean; and observed cost mean. Unknown time/cost is `null` plus observed count, never zero.

For each Testimonium source index, report the same direct baseline. No source is called best and none is merged. The fixed within-candidate evidence is `priming_delta = CER_nuda - CER_primed` and `image_delta = CER_image_absent - CER_primed`.

For a supplied base/checkpoint pair the framework additionally exposes condition-wise base-minus-checkpoint CER and `witness_only_advantage = advantage_primed - advantage_nuda`. These are signs and numbers with no hidden threshold or verdict. They show whether an advantage exists only once Testimonia appear; Tyrel alone interprets them and decides the chair.

## 9. External-vendor and public-evidence gates

An image carries an explicit provenance class: `synthetic`, `cleared_public`, or `private_register`. It is not trusted merely because a caller labels it: the selected manifest seals the class with the exact image/evidence bindings, and the content-addressed run-plan approval seals that manifest. The generic matrix API accepts only local synthetic fakes; it refuses an external-looking fake. A non-synthetic claim can enter only through the declared-roster entry point and a content-addressed run-plan approval. For `private_register`, a current data-gate authority is required; before an external adapter is called, its independently checked `ThirdPartyTransmissionApproval` must bind the exact vendor, resolved candidate artifact digest, page IDs, and manifest. Missing/wrong vendor, snapshot, page set, or approval bytes refuses before a candidate call. Genuinely cleared-public material still needs the manifest-bound run-plan and normalization approvals, but not the private-register vendor transmission approval; it may never be used as an unapproved label for a register image.

Raw requests, prompt bytes, model identities, image bytes/paths, Testimonia, human transcriptions, candidate responses, adjudication records, and limitations stay under approved private roots. They never go to git, `/out`, or `history/`.

A dated finding validates against `history/reading_claim_public_finding.schema.json` and `redaction.validate_public_finding`. It permits only fixed metric keys, integer slots, condition enums, SHA-256 digests, and fixed measure-quote IDs. It requires the exact three-slot × three-condition matrix, matching deltas, equal act denominators, and arithmetic-consistent CER/WER before the only supported writer, `publication.write_public_finding`, performs an exclusive dated write. It has no free-text path. Tests plant synthetic transcript, name, image, and identity fields and prove the projector omits them or validation refuses them. This build writes no finding because it measured nothing.

## 10. Pinned scoring dependencies

| Dependency | Pin | Use | Source and licence |
|---|---:|---|---|
| RapidFuzz | 3.14.5 | Unit-cost Levenshtein distance and edit operations over already-normalized units. | [source](https://github.com/rapidfuzz/RapidFuzz/tree/v3.14.5), [MIT licence](https://github.com/rapidfuzz/RapidFuzz/blob/v3.14.5/LICENSE) |
| uniseg | 0.10.1 | UAX #29 extended grapheme-cluster segmentation after framework normalization. | [source](https://github.com/rivo/uniseg-python/tree/v0.10.1), [MIT licence](https://github.com/rivo/uniseg-python/blob/v0.10.1/LICENSE) |

The framework owns normalization; neither dependency gets to normalize text. Every real
run records these pins and the profile digest.
