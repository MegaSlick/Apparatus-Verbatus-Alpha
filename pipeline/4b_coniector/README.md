# 4b — Coniector

*Conicere*, to conjecture. A text-only stage on a side branch after the Perlector:
beneath each diplomatic reading it proposes a labelled, unconfirmed reconstruction
from the text around the act — the page's other entries and, on a run sealed
`pages_are_consecutive`, the neighbouring pages' edge acts and the other pieces of
an act that crosses a page break. It never sees the page image. The Perlector stays
diplomatic only; the Armarium alone reads what this stage writes.

The chair (`reconstructor`) is the Perlector's model asked text only. Whether it
runs is one sealed switch, `mode` in `config/reconstruction.toml`, and it is off
by default: running it loads a second model.

```sh
.venv/bin/python pipeline/4b_coniector/run.py --run-root <dir> --run-id <id> \
  --reconstruction-config config/reconstruction.toml
```

The records and what the Armarium checks are in [CONTRACT.md](CONTRACT.md).

## Proposed document text

The lead's documents are unchanged by this stage; the text below is proposed for
them.

**GLOSSARY.md, "What the stages produce"**, replacing the entry for
*Reconstruction*:

> **Reconstruction** — a labelled, unconfirmed layer beneath a diplomatic reading,
> proposed by the Coniector from the text around the act, never from the page
> image: departures from the diplomatic text, each with an optional reason, and
> findings (cut at a page break, incomplete, out of sequence, inconsistent) that
> are flags only. It is not an act and not a reading, is counted in no
> denominator, and never replaces the diplomatic reading. Across a page break it
> joins the pieces of one act only on a run sealed `pages_are_consecutive`.

**GLOSSARY.md, the stages table**, a row after *Perlector*:

> | **Coniector** | Proposes a labelled, unconfirmed reconstruction beneath each diplomatic reading from text alone. It establishes nothing; only the Armarium reads it. (*Conicere*, to conjecture.) |

**ARCHITECTURE.md, "The stages"**, after the Perlector:

> **Coniector** — *conicere*, to conjecture. A text-only side branch after the
> Perlector. It reads the established diplomatic readings and proposes, beneath
> each, a reconstruction of what the scribe meant where the text shows a slip, a
> lost abbreviation or an act cut at the page edge. It never sees the image, never
> changes the diplomatic reading, never holds or counts an act, and only the
> Armarium reads it. Findings it makes are flags. Whether it runs is sealed per run.

**ARCHITECTURE.md, "Invariants"**, a new invariant:

> 13. A reconstruction is labelled and unconfirmed, sits beneath the diplomatic
>     reading it departs from, and is never established or counted.

**README.md, the flow diagram and roles table**: the Coniector after the
Archetypus (`Archetypus → Coniector → Armarium`, the Coniector marked as a side
branch), and a row `Coniector | Qwen/Qwen3.8-27B (the Perlector's model, text
only; off by default)`.
