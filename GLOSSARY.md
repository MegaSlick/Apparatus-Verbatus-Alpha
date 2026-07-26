# Glossary — Apparatus Verbatus

One word per concept, one concept per word. If two words mean the same thing, one of
them is wrong.

## The project

**Apparatus Verbatus** — the system. *Apparatus criticus* is the record of variant
readings printed beneath a critical edition; this is both the machinery and that record.
Short form in code and conversation: `verbatus`.

**Ipsissima verba** — "the very words themselves." The textual-criticism term for the
exact original wording. What this project exists to recover.

## Ordinary words that carry weight

**act** — a unit of relevant body text. Usually a register entry: a baptism, marriage,
or burial. But these books also hold index rows, letters, notes, and essays, and the
variance is real. **Deliberately not defined tightly** — a narrow definition excludes
material, and a missed act is worse than a poorly read one.

**page** — one rendered image from the Exemplar.

**crop** — the image region marked out for one act.

**pod** — a rented cloud machine with a GPU. It bills by the hour while it exists.

## The stages

| Term | Latin sense | What it does here |
|---|---|---|
| **Exemplar** | the original one copies from | the sealed, immutable scanned page |
| **Designator** | *designo*, to mark out | finds and bounds the acts on a page |
| **Attestator** | *attestari*, to bear witness | one witness model. Plural: **Attestatores** |
| **Perlector** | *perlegere*, to read through | the trained reader. Reads the ink, establishes the text |
| **Recensor** | *recensio*, review | verifies completeness and drives bounded recovery. Establishes no text |
| **Archetypus** | the reconstructed ancestor | the established reading — the pipeline's output |
| **Armarium** | the cupboard for finished codices | where the output is written |

## The objects

**Testimonium** — one witness's report. Unverified, of uncertain quality, never final,
always retained. Carries the model identity and revision that produced it.

**Lectio** — one reading pass by the Perlector. May be primed by a single witness, or
unprimed.

**Lectio nuda** — an unprimed Lectio. No witness shown. The baseline.

**Perlectio** — what the Perlector returns: the reading, what it was based on, and its
dissent.

## The distinction that matters

**Testimonium** is report. **Autopsia** is sight of the thing itself. The Attestatores
have the first; the Perlector has the second. This is the classical difference between
hearsay and eyewitness, and it is the architectural claim of the whole system.

## Retired terms

These appear in the old repository and mean nothing here. If you see one, it is history.

| Old | Now |
|---|---|
| Stage-W, act reader | **Perlector** |
| picker | *retired, not renamed. Nothing selects among witnesses* |
| consolidator | its witness-voting is **retired**. If an assembled page hypothesis is kept at all, it is a **Testimonium** like any other |
| witness_dai_churro, chandra (as a stage name) | **Attestatores**, **Designator** |
| lean bundle, pilot_*, *_v2 / *_v3 | naming carries meaning, not history |
