# Spec 05 — Perlector reading-claim measurement framework

Status: this is a disposable spike instrument written before this checkout has an
evaluation image, ground-truth transcription, model call, pod, or reading-quality
number. It is not a result. Tests exercise the single candidate interface with
synthetic fakes only.

Spec 08's landing removes this harness. Its protocol and evidence can inform the later
bench and dress rehearsal, but this package is not their implementation and does not
silently turn a spike into durable pipeline architecture.

The declared-run entry point hashes this exact document and compares it with a
reviewable protocol pin in the code. A caller-supplied protocol digest is insufficient:
the sealed manifest and its run-plan approval must name this document's digest.

Spec 05 was built twice, independently and blind, by two seats from different
vendors. This protocol is lane B's, with lane A's human-adjudication procedure,
gap-span handling, direct-import no-transport guard and literature citations merged into it; every
divergence between the two builds and what was done about it is recorded in the merge
report that accompanied this branch. Nothing in either lane ran against real
material, and the merge did not either.

Where the spike's build path departs from spec 05's own text: the spec names
`autoclave/spike_perlector/`, and `autoclave/` was reassigned on 2026-08-01 to mean
the container tooling directory, so this harness lives at `operations/spike_perlector/`.
Both lanes made that same call independently.

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

Dissent is calculated only after candidate text is fixed. It counts comparable and departing Testimonia after the same normalization in every condition, including Lectio nuda; the nuda candidate still receives none of them. It neither changes text nor weights, merges, selects, or rewards a witness.

**The prior on parroting, predeclared with everything else.** Spec 05 records
Tyrel's stated basis that the checkpoint rarely saw witness output in training, so
the prior that it parrots is low. That is written down here, before any material is
shown, because a prior produced *after* results appear is a rationalisation. It
lowers the prior and removes no test: the image-absent control and the nuda/primed
dissent comparison run for every candidate on every act regardless. This paragraph
records spec 05's own sentence, which is the form the basis reached this harness in;
it is not a quotation of Tyrel and does not stand in for one.

A cell with no reading in it records **no** dissent — not dissent from everyone.
This matches the shape the landed `pipeline/4_perlector/run.py` already settles for
the real stage, which publishes an empty dissent record for an act it could not
read. Counting a refusal as departure from every witness would be true as a string
comparison and backwards as a measure: dissent exists to make parroting visible, and
a candidate that refused every act would otherwise read as maximally independent.
The refusal is not lost; it is counted in that cell's response state. And agreement
is not a fault: most lines in a register are easy and every witness agrees, so zero
dissent there is the correct output. A metric that rewarded disagreement would
reward hallucination.

## 4. Sampling frame, seed, and held-out rule

The frame is every Tyrel-approved manually cropped act with opaque act ID, source-page SHA-256, crop SHA-256, provenance class, predeclared century/record/damage stratum, and potential for a checked human reference. No criterion may depend on model output, inter-annotator agreement, cost, or apparent ease. The full pre-reference frame may be selected without text hashes; every selected member must then bind its reference status plus checked-reference, independent-draft, adjudication, and Testimonium hashes before a run can open. `no_readable_text` and `unresolved_gap` must have a private reason-evidence digest in `PrivateSampleAccounting`; that accounting must partition every selected act into scoreable or excluded, is itself in the run-plan approval, and cannot replace a selected act.

The seed is fixed now: `verbatus/spec05/selection-v1`. Within each sealed stratum, sort `SHA-256(UTF-8(seed) || 0x00 || UTF-8(opaque_act_id))`, then take the sealed quota. The quota is Tyrel's GOVERNANCE 9 judgement; the code refuses a missing, altered, or undersupplied quota. It records seed, algorithm, frame digest, selected-member digest, and protocol digest.

The manifest retains the full opaque frame and recomputes its own deterministic draw; a claimed selected list that differs is refused. The declared-run entry point binds every supplied act's opaque ID, source-page digest, crop digest, provenance class, reference status, and private-evidence hash set to exactly that selected list before any dossier or adapter call. The selected crop **and source-page** digests, checked-reference/draft/adjudication evidence, and raw/record/closed-normalization Testimonium hashes are locked by `HeldOutUseGuard` before prompt tuning, padding calibration, adjustment, later bench, or dress rehearsal. A selected payload must be bound to its opaque act before it is transformed; that lineage survives in-framework transforms and prohibited uses refuse regardless of the derived bytes. Bare bytes and bare digest lists refuse for those prohibited uses rather than pretending to prove disjointness. A later stage needs its own separately approved provenance for new material. Locking the page prevents a neighbouring crop from pretending to be disjoint. It mechanically protects callers that enter through this framework; it does not claim to police unrelated manual conduct outside it.

## 5. Ground truth and human adjudication

1. Two qualified people independently make diplomatic transcriptions from the Exemplar crop. They see neither model nor Testimonium output or one another's draft, and mark unread ink as a gap rather than guessing.
2. Preserve both raw drafts privately. Record agreement after the sealed normalizer, but never use agreement to select easy acts or form a majority reading.
3. A third qualified person adjudicates every disagreement against the Exemplar and records a character/span decision or `unresolved_gap`. No model or witness sets the reference.
4. Independent QA checks the reference revision, crop/page digest, and normalization profile digest. The resulting checked reference is immutable; later corrections create a new revision and invalidate comparability rather than overwrite evidence.

**Steps 1–3 are executed by `adjudication.py`, not merely described here.** The
method is the standard one — two independent annotators, differences resolved by a
third and more experienced adjudicator, and material whose disagreement cannot be
reconciled excluded from the gold standard rather than guessed into it — and the
part that makes it checkable is that `disagreement_spans` computes the disputed
spans from the two drafts and `reconcile` then demands **exactly** that set of
spans as the adjudicator's resolution keys. A span left unresolved refuses; a
resolution for a span that does not exist refuses; an adjudicator who is one of the
two transcribers refuses; and an act where nothing readable survives refuses rather
than becoming a checked reference with no ink in it. Both drafts and every
resolution stay on the record unedited beside the reconciled reading (GOVERNANCE 4).
The diff itself is `difflib.SequenceMatcher` from the Python standard library — a
mechanical text diff needs no new dependency, and a deterministic one is what lets
an adjudicator recompute the exact keys that will be demanded of them.

Each draft's digest is taken over the transcriber **and** their text, never over
bare text. Two people transcribing an easy act produce byte-identical text; that is
the good case and must not be refused, while the same draft counted twice must be,
and only a digest that includes the transcriber can tell those two apart.

**A checked reference may carry gaps, and this is the common case, not the edge
case.** Ruling 3, recorded verbatim in `TYREL_RULINGS_2026-08-05.md`: "many of our
records are damaged", and a damaged page yielding three readable words is "a
successful partial reading of three words plus honest gaps — not a failure". A
`GapSpan` marks unread ink at its place in the reference; `start == end` is a
legitimate zero-width anchor, which is the structural reason a gap cannot carry
characters whatever evidence hangs off it. The gap-excised text is what CER/WER uses
as its reference, so unread reference characters never enter its denominator.
**Named limitation:** this is a text-only exclusion, not a spatial alignment. The
scorer cannot locate candidate characters at the gap, so a candidate's guess may
align as an insertion or substitution elsewhere; it cannot be called either correct
or fabricated *inside* the marked gap. That is a harder problem this instrument does
not solve and does not claim to.

`no_readable_text` is a positive fact about a truly blank crop, never an empty string. `unresolved_gap` means ink exists but cannot be adjudicated — for the whole crop; unread ink *within* a reading is a gap, above. Neither has a CER/WER denominator, so neither gets an artificial perfect score. Both remain accounted for in `PrivateSampleAccounting` until a separately predeclared masked-alignment method exists.

**What follows from that, said plainly rather than left to be discovered.** A
selected act whose reference is `no_readable_text` is excluded from the matrix, so
**this instrument does not measure whether a candidate invents text on a blank
page.** That failure mode is named in ruling 3 — "we don't want it making shit up" —
and it is real; it is simply not a measure spec 05 predeclared, and adding it would
change the public finding's shape after the fact, which GOVERNANCE 10 does not
allow. It is a decision for Tyrel, not for this document: either the blank-page
control is predeclared as a measure of its own before an evaluation manifest opens,
or the instrument goes on being honest that it does not take it.

**If he does want it, the shape is already known, and it is not "score the blank act
into CER".** The old pipeline built this measure and its design answers the two
questions this one would face. Read through the window at
`deploy/lean/reader_quality_gate.py` (2026-08-09; understanding carried across, no
line of it): it scores invention as a **separate, explicitly labelled block beside
the accuracy aggregate and never inside it**, on the stated reasoning that a page
which is ten percent probe must not read as a page the reader partly failed. It
splits the question in two rather than averaging them — did the reader reproduce a
legibly-rendered non-word verbatim, or replace it with a real word it recognised
(fabrication by normalisation); and did the reader abstain on a span the page
physically destroyed, or conjure content into the occlusion. And it obtains both by
**constructing probes** — curated non-words and destroyed spans on synthetic canary
pages, with the probe tokens removed from the ordinary checks so nothing is counted
twice — rather than by trying to align a hypothesis against a real gap after the
fact. That last point is the answer to the named limitation above: detecting
fabrication inside a gap is not an alignment problem to be solved, it is a probe set
to be built, and building one is a predeclaration of its own.

## 6. Normalization: recommended `graphemic-v1`

Raw text is retained privately. Apply this sequence identically to reference, candidate, and Testimonium text before CER/WER:

1. Require Unicode text and NFC-normalize it.
2. Replace each Unicode whitespace run with one ASCII space; trim outer spaces.
3. Map U+2018, U+2019, and U+02BC to ASCII apostrophe; map U+2010/U+2011 to ASCII hyphen.
4. Expand only `ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ`; do not apply NFKC or NFKD.
5. In `graphemic-v1`, map long-s `ſ` to `s`.
6. Preserve case, all remaining punctuation, diacritics, digits, historic spelling, abbreviations, `i/j`, `u/v`, and `œ/æ`. Never case-fold, strip accents, modernize, remove punctuation, or correct a scribal error.

CER uses UAX #29 extended grapheme clusters (spaces count). WER uses nonempty runs between canonical spaces, leaving punctuation inside a token. `uniseg==0.10.1` segments clusters; no library normalizer is used. Each run records the profile digest. The conservative historic-spelling choice is supported by the [OCR-D transcription guidance](https://ocr-d.de/en/gt-guidelines/trans/transkription.html), and NFC/reference-length CER by [OCR-D's evaluation specification](https://ocr-d.de/en/spec/ocrd_eval.html).

**Why long-s is the one transliteration applied, and nothing else is.** The
historical long-s is the same letter as round `s`, and folding it is the one
widely-agreed, uncontroversial transliteration in the historical-text-normalization
literature — trivial long-s transliterations are routinely ignored in normalization
comparisons (the large-scale comparison of historical normalization systems,
[arXiv:1904.02036](https://arxiv.org/abs/1904.02036), read 2026-08-05). Everything
past that point is an editorial decision about what the ink *means*: expanding a
scribal abbreviation says what the abbreviation stood for, which belongs to the
transcription convention Tyrel owns (§5), not to a scorer. Over-normalization is a
named risk in that same literature, so this profile stops at the one case with no
real controversy.

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

**Why the bare distance primitive, and why the unbounded denominator.** Both are
predeclared choices rather than a library default inherited without reading it
(GOVERNANCE 10).

*The primitive.* `rapidfuzz.distance.Levenshtein` computes the minimum count of
single-element insertions, deletions and substitutions and nothing else: no
tokenization, no case-folding, no text normalization of any kind. That is exactly
why it is called directly instead of a higher-level scorer such as `jiwer`
(Apache-2.0, RapidFuzz-backed underneath), which bundles its own tokenization and
transform pipeline into `wer()`/`cer()`. What `jiwer` normalizes by default could
not be confirmed from its own documentation in one reading (checked 2026-08-05: the
usage page defers the default-transform list to a separate API reference). Rather
than call a function whose normalization has not been read, the distance primitive
is used bare and **every** normalization decision above is this project's own,
explicit, and independently tested.

*The denominator.* Two conventions exist. The classical / NIST-style rate used here
is `(S + D + I) / N` with `N` the reference length, which is unbounded above.
OCR-D's own evaluation specification normalizes instead — `(I + S + D) / (I + S + D
+ C)` — which is bounded to `[0, 1]`. The reason for choosing the unbounded form is
concrete and ties to ruling 3, "we don't want it making shit up": a refused,
missing, unavailable or malformed response scores an empty hypothesis, which against
a non-blank reference is exactly `1.0`. Under the bounded formula, a candidate that
hallucinates an enormous volume of wrong text *approaches but can never exceed* that
same ceiling, so fabrication is capped at parity with honest silence. Under the
unbounded formula, wild fabrication scores **worse** than declining to answer. Given
this project's explicit concern about invention on damaged ink, the formula that
leaves that penalty uncapped is the one that measures honestly. Nothing here rewards
silence over an honest attempt either: a partial, flawed reading almost always
scores below 1.0 and therefore better than declining.

| Observed response state | CER/WER hypothesis | Extra accounting |
|---|---|---|
| complete | supplied non-blank text | complete count |
| truncated | supplied partial text | truncation count; missing tail deletes |
| no_readable_text | empty scoring hypothesis, no text on the record | explicit blank-finding count; all checked reference units delete |
| refused, missing, unavailable, malformed | empty scoring hypothesis | named state count; all reference units delete |
| unproved adapter delivery | none | invalidate the measurement; do not score a harness defect as a model failure |

A refusal cannot improve by disappearing. Synthetic hand-worked tests pin exact match, one substitution/deletion/insertion on `abc`, empty against `abc`, two-error WER, composed/decomposed acute, whitespace, and preservation of case/punctuation/diacritics.

**Response and text bounds.** Every field this instrument hashes, aligns, or segments -- a candidate response, a Testimonium, a checked reference, an adjudicator's resolution -- is refused above 20,000 characters (`MAX_TEXT_LENGTH`) and above 30 combining marks stacked on one character (`MAX_COMBINING_RUN`), and refused outright if it contains an unpaired UTF-16 surrogate. GLOSSARY's *act* is deliberately broad, but none of index rows, letters, notes, or essays approaches either bound; a field over it is a mis-pasted file, not a reading, and scores `malformed` rather than being measured. The character bound keeps `score_text` and `disagreement_spans` off their quadratic worst case (measured 2026-08-09: `disagreement_spans` costs up to ~1 minute of CPU at the 20,000-character bound on adversarial input); the combining-mark bound exists because `uniseg` grapheme segmentation is quadratic in one cluster's own length regardless of total text length.

## 8. Measures and evidence, never a picker

For each candidate slot × condition, report CER/WER numerator, denominator, and rate; character completeness; every state count; structural dissent counts/rate; wall-time mean; and cost mean. A publishable run requires a wall-time and cost observation for every candidate × act × condition cell. Synthetic interface exercises may leave either unknown, but they cannot become a finding.

For each Testimonium source index, report the same direct baseline. No source is called best and none is merged. The fixed within-candidate evidence is `priming_delta = CER_nuda - CER_primed` and `image_delta = CER_image_absent - CER_primed`.

For a supplied base/checkpoint pair the framework additionally exposes condition-wise base-minus-checkpoint CER and `witness_only_advantage = advantage_primed - advantage_nuda`. These are signs and numbers with no hidden threshold or verdict. They show whether an advantage exists only once Testimonia appear; Tyrel alone interprets them and decides the chair.

## 9. External-vendor and public-evidence gates

An image carries an explicit provenance class: `synthetic`, `cleared_public`, or `private_register`. It is not trusted merely because a caller labels it: the selected manifest seals the class with the exact image/evidence bindings, and the content-addressed run-plan approval seals that manifest. The generic matrix API accepts only local synthetic fakes; it refuses an external-looking fake. A non-synthetic claim can enter only through the declared-roster entry point and a content-addressed run-plan approval. For `private_register`, a current data-gate authority is required; before an external adapter is called, its independently checked `ThirdPartyTransmissionApproval` must bind the exact vendor, resolved candidate artifact digest, page IDs, and manifest. Missing/wrong vendor, snapshot, page set, or approval bytes refuses before a candidate call. Genuinely cleared-public material still needs the manifest-bound run-plan and normalization approvals, but not the private-register vendor transmission approval; it may never be used as an unapproved label for a register image.

Raw requests, prompt bytes, model identities, image bytes/paths, Testimonia, human transcriptions, candidate responses, adjudication records, and limitations stay under approved private roots. They never go to git, `/out`, or `history/`.

A dated finding conforms to `reading_claim_public_finding.schema.json`, which lives beside this document rather than in `history/` — `history/README.md` says that directory holds dated evidence and that anything there telling you what to do is out of date by definition, and a schema is exactly a document that tells you what to do. The finding itself is still written into `history/`. That schema is the published shape, for a reader checking a finding without this code; nothing here executes it, and this framework takes on no JSON-Schema dependency to do so. What runs before every write is `redaction.validate_public_finding`, deliberately the stricter of the two, and a test pins both to the same closed key sets and enumerations so they cannot drift apart unnoticed. It permits only fixed metric keys, integer slots, condition enums, SHA-256 digests, and fixed measure-quote IDs. It requires the exact three-slot × three-condition matrix, matching deltas, equal act denominators, and arithmetic-consistent CER/WER before the only supported writer, `publication.write_public_finding`, performs an exclusive dated write. It has no free-text path. Tests plant synthetic transcript, name, image, and identity fields and prove the projector omits them or validation refuses them. This build writes no finding because it measured nothing.

## 10. Pinned scoring dependencies

| Dependency | Pin | Use | Source and licence |
|---|---:|---|---|
| RapidFuzz | 3.14.5 | Unit-cost Levenshtein distance and edit operations over already-normalized units. | [source](https://github.com/rapidfuzz/RapidFuzz/tree/v3.14.5), [MIT licence](https://github.com/rapidfuzz/RapidFuzz/blob/v3.14.5/LICENSE) |
| uniseg | 0.10.1 | UAX #29 extended grapheme-cluster segmentation after framework normalization. | [source](https://github.com/rivo/uniseg-python/tree/v0.10.1), [MIT licence](https://github.com/rivo/uniseg-python/blob/v0.10.1/LICENSE) |

The framework owns normalization; neither dependency gets to normalize text. This
protocol and the repository lock record the dependency pins; every real run records the
profile digest and binds this protocol by digest.
