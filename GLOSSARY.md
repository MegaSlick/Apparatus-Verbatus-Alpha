# Glossary

One word per concept, one concept per word. The stage names are Latin, borrowed from
manuscript practice and textual criticism; each entry gives the plain meaning used here.

## The project

**Apparatus Verbatus** — the system. An *apparatus criticus* is the record of variant
readings printed beneath a critical edition; this project is both the machinery and that
record. Short form in code: `verbatus`.

**Ipsissima verba** — "the very words themselves": the exact original wording, which is
what the project exists to recover.

## Everyday words with a specific meaning

**act** — one registered act: a baptism, marriage, burial or other act entered in a
parish or civil register, with its margin note and signatures. A notarial act or
contract (an *instrument*) counts as an act for "never lose an act". Index rows, table
rows, ledger entries and paragraphs of running text are entries of their own kind, read
and accounted for like acts but never counted as acts.

**page type** — what kind of document a page is, as the Perlector names it:
`register-acts`, `index`, `table`, `ledger`, `instrument`, `prose` or `blank`. It decides
which checks apply to the page; detection facts (Surya's and the witnesses' `Table`
labels, the record detector's count) are recorded beside it as a cross-check.

**entry kind** — what one entry of a page reading is: `act`, `index-row`, `table-row`,
`ledger-entry`, `instrument`, `paragraph` or `other`. An `act` or `instrument` is of the
act class; every other kind is of the `other` class, which is what later records call
the entry's `kind`. Rows of an index, table or ledger are exported as rows, not acts.

**entry kind `other`** — an entry of a page reading that is text but no entry of the
page's own kind: a heading, a page number, a marginal note that is not an entry. Every
entry of the `other` class is read, placed and accounted for like an act but is not
counted as one, and a page whose entries are all of that class is held until the
Recensor confirms it holds no act.

**page** — one image from the source.

**crop** — an image region cut from a sealed page and shown or kept: one detected
record's crop for a record reader, or an act's reading region.

**witness** — a model that reads every page and reports what it saw: a whole-page reader
reads the page image, a record reader reads the crops of the records its detector found
on the page. Its report is evidence, not an answer.

**witness routing** — seating a witness on some pages only. A models roster's
`[witness_routing]` names the chair and its rule; the one rule, `index-and-table.v1`,
sends dots.mocr a page when Surya tags a `Table` block on it or the record detector
finds no record on it. The decision is read from the Designator's records before any
witness reads the page, sealed per page and summarised in
`run-health/witness-routing.json`. On a page not routed to it the chair is not part of
the page's roster at all. No committed roster routes anything.

**witness floor** — how many configured witnesses must have read a page, without
truncation, for a unit on it to be accepted. Set as `witness_floor` in the model roster
and checked by the Recensor; a page below it is held `under-witnessed`. It counts the
page's own roster: a routed witness counts on the pages routed to it and nowhere else.

**chair** — a named role in the pipeline that one model fills. The binding lives in a
model roster under `config/`: `models.toml` holds small local stand-ins and
`models-real.toml` the real models. A model of a family the code already has an adapter
for can be swapped by configuration alone; a new witness family needs an adapter. The
chairs:

| Chair | Role |
|---|---|
| `attestator_1`, `attestator_2`, `attestator_3` | the witnesses (Attestatores) |
| `secondary_proposer` | the record detector: the Designator's detector of register records, whose crops the record reader reads |
| `designator_surya` | Surya, the Designator's line and layout detector |
| `perlector` | the reader |
| `reconstructor` | the Coniector's chair: the Perlector's model, asked text only |
| `annotator` | reserved for a semantic annotation layer; declared absent in the real roster, and no stage uses it |

**door** — the intake step before the Exemplar: it checks and seals what was submitted,
and records anything it refuses.

**triage** — optional work before the door: how each photographed frame is split into
pages, cropped, deskewed and converted, and which frames are captures of the same leaf.
It is not a stage and never runs in a run; its decisions reach the door as documents
(`pipeline/0_triage/CONTRACT.md`), and it never picks among captures.

**canary** — a private page whose reading is already known, added to a real run as a
control from a separate folder and manifest. The door gives canary pages the ordinals
after the real pages and seals their ledger as `canary-ledger`. A canary page is never
read beside a real one (never a side of a page break, never a reconstruction's context),
and the export reports canaries apart from the delivered pages.

**sealed** — written once with a recorded hash, so an accidental later change is detectable.

**run authority** — `run.json`, the self-hashed record of what one run is: its source
pages, witness chairs, configuration digest and the digest of every sealed configuration.
Reopening a run id whose bindings have changed is refused.

**stage seal** — the `stage-seal` record a stage publishes, with its `decode-environment`,
before its final manifest. It binds what the stage wrote to the run's configuration; the
next stage refuses to start without it. A stage that held after publishing its evidence
still seals; one that stopped before does not.

**unit** — two uses, kept apart by context. A *witness unit* is one piece of a witness's
page in that witness's own order (a Chandra block, a Churro section, one of DAI's record
crops), cited by an id such as `A1`. A *review unit* is one row the run counts: an entry
of a page reading, or the one row standing for a page with none (unread or blank); the
Recensor reviews each and a person decides about held ones (`--unit p1:2`).

**held** — kept back for human review: not established or exported until a person
decides, and never silently dropped or passed as done.

**flagged** — marked for human review without being held: a finding the sealed policy
names as a review flag is measured and recorded like a hold, reported, and carried with
the reading's text in the flagged export, but the reading is established and exported
as read. A flag is a question for a person; a hold is a stop.

**override** — a person's decision (`release`) that sends a held reading to export as
the model read it, its own holds included. It is labelled "released by operator" with
who, when, why and the holds it cleared. It is never available for a reading with no
place on the page, malformed doubt marks or no text.

**correction** — a person's corrected text for a held reading (`edit`), bound to the
reading it corrects. Once the Recensor accepts it, the person's text is the established
reading, taken as the truth with no machine doubt, labelled "corrected by a person" with
who, when, why and an optional note; the model's reading stays beside it in the export as
"model reading (original)". It is delivered and counted like any accepted act and never
by itself makes a run partial.

**operator re-read** — a person's request (a page `re-ask` decision) that the Perlector
read a held page again. It is outside the re-ask budget, its acts are labelled "read on
operator re-read", and it becomes the page's current reading; earlier readings stay,
marked superseded.

**systemic hold** — a run in which more than a sealed share of the pages (1 in 50) are
held after the Recensor. Such a run has a problem of its own, not just a few hard pages,
and says so in its stop report, its export and its notification.

**failed page** — a page whose reading call failed (the engine was unreachable, refused
or stopped unrecognised). It is recorded as a `failed` page reading, held as unread, and
counts toward the run's hard-failure cap. A page the door could not decode (corrupt or
unreadable) is not a failed page but a refused one: recorded as a `page-refused` row,
never counted as acts and not held, and it also counts toward the cap. Neither is ever
counted as an empty page.

**blank page** — a page confirmed to hold no text: the reader found none, the page
accounting finds no ink left unread, the record detector found no record, no line was
detected, at least one witness read it, and every witness that read it found no text.
A page the reader reads as
having no text is held until all of that is confirmed; ink with no reading is never
passed as blank.

**re-ask** — one further, bounded question to the reader about a page whose first reading
left named ink unread: a witness unit neither cited nor set aside, or a detected line or
detector record outside every region. It is additive and separately recorded; acts it
recovers are labelled "read on re-ask". The bound is sealed per run.

**run tree** — the directory one run writes, with one folder per stage.

**pod** — a rented cloud machine with a GPU, billed by the hour while it exists.

## The stages

| Term | Plain meaning here |
|---|---|
| **Exemplar** | The sealed, immutable source page. (In manuscript practice, the original a scribe copies from.) |
| **Ink map** | Measures where ink lies on each sealed page, without any model, so the page accounting can check that every inked region ended up in a reading region. |
| **Designator** | Publishes each page's detected lines, blocks and records, the evidence the reading is checked against. It marks out no act and establishes no text. |
| **Attestator** | One witness model. Plural **Attestatores**. |
| **Perlector** | The reader: reads each whole page itself, names the acts on it and establishes their text, using witness testimony as clues. |
| **Coniector** | Proposes a labelled, unconfirmed reconstruction of an act from the text around it: the Perlector's transcriptions, never the image. Establishes nothing. (In textual criticism, a conjecture is a reading no witness carries.) |
| **Recensor** | Checks that each page is completely accounted for and holds what is not. It establishes no text and calls no model. (Textual critics use *recensio* for weighing witnesses; here the word means the completeness review.) |
| **Archetypus** | The established reading, the pipeline's output: a machine reading, not truth. (Borrowed loosely from the ancestor text all witnesses descend from.) |
| **Armarium** | Where the output is written. (The cupboard where finished books were kept.) |

## What the stages produce

**Testimonium** (plural *Testimonia*) — one witness's unverified report on one page, of
uncertain quality, always kept and never final; it names its model and revision.

**feed** — everything one whole-page Perlector call is shown, built from sealed records
under the run's sealed `[feed]` switches: the page image, each shown witness's page
broken into its own units (ids `A1`, `B1`, …), and Surya's detected lines (`L1`, …) and
blocks (`S1`, …). It defines every id the reading may cite and is published as a
`page-feed` before the call.

**page reading** — one Perlector answer for a whole sealed page, recorded as a
`page-reading`. A page has its first reading and may have one re-ask and operator
re-reads, each a separate attempt; none overwrites another. An operator re-read
supersedes the earlier readings, which stay in the run tree, and becomes the page's
current reading. A first reading that is not a parsed, valid answer is held as unread.

**set-aside** — an id from the feed that the reading lists in its answer's `set_aside`,
with a short reason, instead of citing it, because there is nothing to read there
(empty, not text, or a detection that repeats another). The page accounting counts it
as accounted for, but holds the page when a set-aside witness unit carries more text
than the sealed limit, when it names a detector record, or when a re-ask sets aside an
id it was asked about.

**page accounting** — the model-free check, recorded as a `page-accounting`, that a page
reading accounted for everything on its page: every witness unit cited or set aside,
every detected line inside a reading region or set aside, every detector record inside a
reading region, every witness's text read, and no ink left outside the regions. A Surya
block places nothing. Whatever it cannot account for, or cannot
measure, holds the page.

**denominator** — what a run counts, derived from the Perlector's sealed records with
the page accounting recomputed, never taken from a record: one page row per sealed page,
and one review-unit row per entry of each sealed page's current reading, or one row
standing for a sealed page with none. A page the door refused appears only as a
`page-refused` row, recorded and never counted. The Recensor, the Archetypus and the
Armarium all count by it.

**reading region** — the boxes an act's citations cover on its page; their union is the
act's region.

**Lectio nuda** — a reading made with no witness shown. The pipeline never establishes
one: the stages after the Perlector refuse it.

**Perlectio** — what the Perlector returns for one entry: the reading, what it was based
on, and where it departed from every witness (its dissent).

**Diplomatic** — the Perlector's transcription of exactly what the ink on one page
shows, read with the witnesses as clues and never corrected from context. It is the
established reading unless a person corrected it.

**Reconstruction** — a labelled, unconfirmed layer under a diplomatic, made after the
reading by the Coniector or a person from the text around the act: the diplomatic with
its departures applied, and, on a run whose pages are declared consecutive, an act's
pieces joined across a page break. Not an act and not a reading; never established,
never counted.

**departure** — one change a reconstruction makes to the diplomatic, recorded with its
span, both readings and, optionally, a reason; not dissent.

## The distinction that matters

**Testimonium** is report; **autopsia** is seeing the thing itself. Witnesses and reader
may both look at the page, but only the reader's role is to establish the text from the
ink. That difference in role is the design of the whole system.

**picker** — any step that chooses among witness readings. There is none, by design
(PRINCIPLES.md, "The idea").
