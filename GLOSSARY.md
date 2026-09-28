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

**crop** — the image region marked out for one act.

**witness** — a model that reads an act and reports what it saw. Its report is
evidence, not an answer.

**chair** — a numbered role in the pipeline that one model fills. The binding lives in a
model roster under `config/`: `models.toml` holds small local stand-ins and
`models-real.toml` the real models. *Attestator 1* is a chair; the model sitting in it
can be swapped without touching code.

**door** — the intake step before the Exemplar: it checks and seals what was submitted,
and records anything it refuses.

**sealed** — written once with a recorded hash, so an accidental later change is detectable.

**held** — set aside for human review rather than silently dropped or passed as done.

**run tree** — the directory one run writes, with one folder per stage.

**pod** — a rented cloud machine with a GPU, billed by the hour while it exists.

## The stages

| Term | Plain meaning here |
|---|---|
| **Exemplar** | The sealed, immutable source page. (In manuscript practice, the original a scribe copies from.) |
| **Ink map** | Measures where ink lies on each sealed page, without any model, so the Recensor can check that every inked region ended up in an act. |
| **Designator** | Finds the acts on a page and marks their bounds. It may use textual cues, but never establishes the text. |
| **Attestator** | One witness model. Plural **Attestatores**. |
| **Perlector** | The reader: reads the ink itself and establishes the text, using witness testimony as clues. |
| **Recensor** | Checks that the page is completely covered and drives bounded recovery. It establishes no text. (Textual critics use *recensio* for weighing witnesses; here the word means the completeness review.) |
| **Archetypus** | The established reading, the pipeline's output: a machine reading, not truth. (Borrowed loosely from the ancestor text all witnesses descend from.) |
| **Armarium** | Where the output is written. (The cupboard where finished books were kept.) |

## What the stages produce

**Testimonium** (plural *Testimonia*) — an unverified witness report on an act,
of uncertain quality, always kept and never final; it names its model and revision.

**Lectio** — one reading pass by the Perlector, either shown witness testimony
(primed) or not.

**Lectio nuda** — a sampled unprimed baseline, separate from the universal Pass-A
draft (*lectio-prior*); both see the same inputs and measure sampling variance.

**Perlectio** — what the Perlector returns: the reading, what it was based on, and where
it departed from every witness (its dissent).

**Reconstruction** — a labelled, unconfirmed export layer that joins two delivered
literal page readings at a page break (head, one newline, tail, nothing else changed);
it is not an act and not a reading, and never counts toward the act total.

## The distinction that matters

**Testimonium** is report; **autopsia** is seeing the thing itself. Witnesses and reader
may both look at the page, but only the reader's role is to establish the text from the
ink. That difference in role is the design of the whole system.

**picker** — any step that chooses among witness readings. There is none, by design
(PRINCIPLES.md, "The idea").
