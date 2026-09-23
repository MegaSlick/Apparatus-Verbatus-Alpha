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

**chair** — a numbered role in the pipeline that one model fills, bound to that role only
in `config/models.toml`. *Attestator 1* is a chair; the model sitting in it can be
swapped without touching code.

**pod** — a rented cloud machine with a GPU, billed by the hour while it exists.

## The stages

| Term | Plain meaning here |
|---|---|
| **Exemplar** | The sealed, immutable source page. (In manuscript practice, the original a scribe copies from.) |
| **Designator** | Finds the acts on a page and marks their bounds. It may use textual cues, but never establishes the text. |
| **Attestator** | One witness model. Plural **Attestatores**. |
| **Perlector** | The reader: reads the ink itself and establishes the text, using witness testimony as clues. |
| **Recensor** | Checks that the page is completely covered and drives bounded recovery. It establishes no text. (Textual critics use *recensio* for weighing witnesses; here the word means the completeness review.) |
| **Archetypus** | The established reading, the pipeline's output: a machine reading, not truth. (Borrowed loosely from the ancestor text all witnesses descend from.) |
| **Armarium** | Where the output is written. (The cupboard where finished books were kept.) |

## What the stages produce

**Testimonium** — one witness's report on an act: unverified, of uncertain quality,
never final, always kept. It carries the identity and revision of the model that made it.

**Lectio** — one reading pass by the Perlector, either shown witness testimony
(primed) or not.

**Lectio nuda** — an unprimed reading, with no witness shown. The baseline that shows
whether the reader can read without help.

**Perlectio** — what the Perlector returns: the reading, what it was based on, and where
it departed from every witness (its dissent).

## The distinction that matters

**Testimonium** is report; **autopsia** is seeing the thing itself. Witnesses and reader
may both look at the page, but only the reader's role is to establish the text from the
ink. That difference in role is the design of the whole system.

**picker** — any step that chooses among witness readings. There is none, by design
(PRINCIPLES.md, principle 1).
