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

**act** — one unit of body text, usually a register entry (a baptism, marriage or
burial). Registers also hold index rows, letters and notes, so the term is kept
deliberately loose: a narrow definition would exclude material, and a missed act is worse
than a poorly read one.

**page** — one image from the source.

**crop** — an image region cut from a sealed page and shown or kept: one detected
record's crop for a record reader, or an act's reading region.

**witness** — a model that reads a page, or each record on it, and reports what it
saw. Its report is evidence, not an answer.

**chair** — a numbered role in the pipeline that one model fills. The binding lives in a
model roster under `config/`: `models.toml` holds small local stand-ins and
`models-real.toml` the real models. *Attestator 1* is a chair; the model sitting in it
can be swapped without touching code.

**door** — the intake step before the Exemplar: it checks and seals what was submitted,
and records anything it refuses.

**sealed** — written once with a recorded hash, so an accidental later change is detectable.

**held** — set aside for human review rather than silently dropped or passed as done.

**failed page** — a page whose reading could not be made: the page did not load or the
reader's call failed. It is held and counts toward the run's failure cap; it is never
counted as an empty page.

**blank page** — a page confirmed to hold no text: the reader found none, the ink map
shows no ink left unread, no line was detected, and every witness that read it found no
text. A page the reader reads as having no text is held until all of that is confirmed;
ink with no reading is never passed as blank.

**re-ask** — one further, bounded question to the reader about a page whose first reading
left named ink unread. It is additive and separately recorded; acts it recovers are
labelled "read on re-ask". The bound is sealed per run.

**run tree** — the directory one run writes, with one folder per stage.

**pod** — a rented cloud machine with a GPU, billed by the hour while it exists.

## The stages

| Term | Plain meaning here |
|---|---|
| **Exemplar** | The sealed, immutable source page. (In manuscript practice, the original a scribe copies from.) |
| **Ink map** | Measures where ink lies on each sealed page, without any model, so the Recensor can check that every inked region ended up in a reading region. |
| **Designator** | Publishes each page's detected lines, blocks and records, the evidence the reading is checked against. It marks out no act and establishes no text. |
| **Attestator** | One witness model. Plural **Attestatores**. |
| **Perlector** | The reader: reads each whole page itself, names the acts on it and establishes their text, using witness testimony as clues. |
| **Coniector** | Proposes a labelled, unconfirmed reconstruction of an act from the text around it: the Perlector's transcriptions, never the image. Establishes nothing. (In textual criticism, a conjecture is a reading no witness carries.) |
| **Recensor** | Checks that the page is completely covered and holds what is not. It establishes no text. (Textual critics use *recensio* for weighing witnesses; here the word means the completeness review.) |
| **Archetypus** | The established reading, the pipeline's output: a machine reading, not truth. (Borrowed loosely from the ancestor text all witnesses descend from.) |
| **Armarium** | Where the output is written. (The cupboard where finished books were kept.) |

## What the stages produce

**Testimonium** (plural *Testimonia*) — an unverified witness report on a page,
of uncertain quality, always kept and never final; it names its model and revision.

**page reading** — the Perlector's one whole-page answer for a sealed page. A page whose
reading cannot stand is held as unread.

**reading region** — the boxes an act's citations cover on its page; their union is the
act's region.

**Lectio nuda** — an unprimed reading with no witness shown: an experiment in
`operations/spike_perlector/`, not a pipeline pass.

**Perlectio** — what the Perlector returns: the reading, what it was based on, and where
it departed from every witness (its dissent).

**Diplomatic** — the Perlector's transcription of exactly what the ink on one page
shows, read with the witnesses as clues and never corrected from context; the
established reading.

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
