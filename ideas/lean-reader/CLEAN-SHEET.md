# Clean sheet: what I would build if starting fresh

**ACTIVE IDEA. Fresh-eyes design, not bound by this repository's contracts or
governance documents; possibly a separate project. Do not delete without the lead's
approval.**

The lead asked for fresh thinking, not a retrofit. This page ignores how the current
pipeline is built and asks one question: *what is the fastest, cheapest, most private
way to get full diplomatic readings of handwritten registers (every act, every margin
note, every godparent) on hardware the lead owns or rents by the hour?* Figures come
from the research in [README.md](README.md) and [SOURCES.md](SOURCES.md).

## The four ideas it rests on

1. **Read lines, not pages.** Every fast, cheap handwriting system that has worked at
   archive scale (BALSAC's 2M pages, FamilySearch, the PRHLT indexes, Transkribus) reads
   lines. A line is small, so tiny models read it; lines are independent, so they spread
   across any number of machines; and a line is where uncertainty is local and fixable.
   Whole-page vision reading is the expensive exception, kept for the few places that
   need the page's context.
2. **Registers are formulas with blanks.** Most of an act is the priest's formula
   (*L'an mil sept cent..., par nous prêtre soussigné, a été baptisé...*). The formula is
   easy and nearly every reader gets it right. The value and the difficulty are in the
   blanks: names, dates, places, relationships, margins. Spend effort there, not evenly.
3. **Ask the expensive model to choose, not to write.** A model asked "which of these
   three readings does the ink show, or none?" about one line crop answers in a few
   tokens. A model asked to transcribe a page writes thousands. Most of the cost of the
   big reader today is writing; a well-built pipeline mostly asks it to choose.
4. **The labelling loop is the product.** Models will keep changing; the asset that
   lasts is a growing set of checked lines from these hands and a loop that turns a
   person's ten-minute correction into a better model by the next morning.

## The machine

```
 images ──► Layout ──► Line crops ──► Readers (swarm) ──► Lattice ──► Adjudicator ──► Act builder ──► Output
            pages,     body lines,    cheap, diverse,     every        small VLM        formula +        diplomatic text,
            acts,      margins,       several image       candidate,   chooses on       slots: names,    structured act,
            margins,   signatures     views each          with its     disputed         dates, kin,      confidence per
            signatures                                    score        spans only       godparents       character, links
                                                                           │
                                                                           └──► Human queue (what no machine settles)
```

### 1. Layout (GPU or CPU, under a second a page)

One trained detector finds act blocks, body lines, **margin lines**, signatures and
crossings-out. Train it on these registers; a YOLO-class detector learns a register's
layout from a few hundred annotated pages and runs in tens of milliseconds a page on a
GPU. Margins are a first-class class from day one, not an afterthought, because they are
what every body-text detector drops. An ink check (inked pixels covered by no detected
region) runs on every page and sends uncovered ink to the human queue.

### 2. Image views (CPU, milliseconds)

Each line crop is rendered three or four ways: as scanned, grey with contrast stretched,
binarised (Sauvola), and the colour channel with the most ink contrast. Faded iron-gall
ink, bleed-through and stains each favour a different view. Views are free compared with
models and give readers genuinely different inputs.

### 3. The reader swarm (most of the compute, all of it small)

Diversity matters more than count. A useful swarm has three different kinds of reader,
each on two views:

| Reader | Size | Where it runs | What it brings |
|---|---|---|---|
| CTC line recogniser (PyLaia or kraken), fine-tuned on these hands | 5-20M | CPU: about 0.03 s a line | fast, honest letter-level errors, per-character scores |
| Second CTC recogniser, different architecture or training data (PP-OCRv6) | 16M | CPU or GPU | errors uncorrelated with the first |
| Small vision-language line reader (Qwen 2-4B with a LoRA), 8-bit | 2-4B | one GPU, or a Mac through MLX | reads abbreviations and context the CTC models miss |

Every reader writes not just its best line but its top candidates with scores. No reader
is trusted alone; the point is that they disagree in different places.

**Hand-adaptive readers.** Writer identification is cheap (a small embedding model on
line images clusters pages by hand). Use it twice: to pick which pages a person checks
(one or two from every hand cluster nobody has checked yet), and to fit a tiny per-hand
adapter for the CTC model from those few pages, in minutes. Each priest's hand gets its
own fine-tune for pennies.

### 4. The lattice (CPU, microseconds a line)

Align every candidate from every reader and view into one character-level confusion
network per line: at each position, the possible characters and the evidence for each.
Most positions have one candidate everyone agrees on; a few have two or three. This is
the whole page's uncertainty in a compact, inspectable form, and it is kept in the
output, not thrown away.

### 5. The adjudicator (small GPU budget, only where needed)

Only disputed spans go further. For each, a vision-language model sees the line crop
(in its best view, zoomed) and the candidates, and answers with a choice, "none of
these" plus its own reading, or "cannot read". Short answers make this cheap, and a 7-9B
model at 8 bits fine-tuned for exactly this task should do it well; a bigger model or a
frontier API is the fallback for what it marks "cannot read". The adjudicator never sees
an act it was not asked about, so a page with one hard name costs one short call, not a
page transcription.

Guarding against the shared-prior trap: the adjudicator is sometimes shown spans where
all readers agreed, chosen at random and with planted alternatives, and its rate of
catching a wrong unanimous reading is tracked. When readers agree on a modernised
spelling or a "corrected" date, this is where it is caught.

### 6. The act builder and the specialists (CPU, milliseconds)

This is where the lead's swarm of oddly specific tools lives, as small programs and
small models working on text and structure, never overwriting what the ink says:

- **Formula matcher:** aligns each act to its type's formula and pulls out the slots:
  child, parents, godparents, witnesses, priest, date, place, margin annotation. A slot
  the formula expects and the reading lacks is flagged (a dropped godparent shows up
  here, not in a character error rate).
- **Date reader:** parses dates written in words and abbreviations (*7bre*, *Xbre*),
  checks the sequence against neighbouring acts, flags jumps.
- **Corpus as its own gazetteer:** names recur across acts in a parish: the same
  families, godparents, priests and notaries appear hundreds of times. Every name read
  with high confidence goes into a per-parish index, and an uncertain name that matches
  a known one becomes a ranked hint for the adjudicator and a link in the output, while
  the diplomatic text keeps the spelling on the page. No outside database is needed;
  the registers supply their own.
- **Kinship checks:** a godmother who is also the mother, a burial before the baptism, a
  child baptised twice: cheap contradictions that point at misreadings or genuinely
  interesting records, both worth a human look.
- **Historical and regional context model:** a small text model, fine-tuned on period
  French and on the region's history, that explains or flags (an unfamiliar place name
  is a known seigneurie; a title belongs to the period) for the reviewer. It annotates;
  it never edits.

### 7. Output

Each act comes out as the diplomatic text (exactly as written, abbreviations and
spelling kept, doubt marked), a structured record (people, roles, dates, places), a
confidence for every character, the lattice of alternatives, and coordinates back to
the image for every word. Because the alternatives are kept, the whole archive is
searchable even where the reading is uncertain (a search for *Tremblay* finds the act
whose best reading is *Tremblai*), which is what probabilistic indexing showed works at
archive scale.

### 8. The human queue and the flywheel

Everything no machine settles (uncovered ink, "cannot read", failed checks, a sample of
unanimous spans) goes to one queue, sorted by how much a correction teaches: hands with
no checked lines first, then the most common confusions. Each correction is a training
line. Every night the CTC readers and the per-hand adapters retrain on the day's
corrections, and the next day's queue is shorter. That loop, not any single model, is
what makes the system better than a frontier model on these registers, and it keeps
working when frontier models change.

## Running it

- **On a cluster, for speed.** Everything after layout is per line and stateless, so it
  shards trivially: images in shards, a job queue (Ray or a plain task queue), CPU nodes
  for the CTC readers and the specialists, a few GPUs for layout, the VLM line reader and
  the adjudicator. The CTC tier reads 120k pages in a few hours on a modest CPU fleet or
  about two hours on one GPU; the GPU tiers scale with how much is disputed. A few
  hundred gigabytes of images becomes a matter of how many nodes are rented for an
  afternoon.
- **On a Mac mini, for privacy.** The same code. Layout and the VLM line reader through
  MLX on the Apple GPU, the CTC readers on the CPU cores, the adjudicator at 8 bits.
  Slower, but private, steady, and able to work through an archive's backlog at night
  without anything leaving the building. Image prefill is the Mac's weak point, which
  is why line crops suit it.

## Why this could be worth a project of its own

- **There is a gap.** FamilySearch's full-text search covers English, Spanish and
  Portuguese; Drouin's millions of Quebec documents are digitised but mostly
  untranscribed; BALSAC indexed fields, not full readings. Full, checkable French
  readings of these registers, margins and godparents included, do not exist at scale.
- **Archives want it on their own machines.** Institutions holding records with personal
  data on people who may still be living (later 19th and 20th century registers) are
  often unable or unwilling to send them to a foreign API; Quebec's privacy law (Law 25)
  adds weight to that. A tool that runs on one Mac mini in the archive is a different
  offer from a cloud service. *Check the legal specifics before relying on this.*
- **It generalises.** Layout, line swarm, lattice, adjudicator and a formula-driven act
  builder fit any formulaic handwritten series: censuses, notarial acts, ledgers, civil
  registers in other languages. Only the formulas and the checked lines change.

## What would kill it

- If the small vision-language line reader plus the CTC models cannot get close to a
  frontier model on these hands even after a few hundred checked pages, the swarm is a
  slower way to call an API. The first experiment must be exactly that comparison.
- If margin notes and signatures defeat line detection, the line-first design loses the
  things the lead cares about most. Layout is the riskiest component and should be
  tested first, on the hardest pages.
- If the lead cannot spend time checking lines, the flywheel does not turn. Ten minutes
  a day of well-chosen corrections is the assumption.

## The first week, if this were a new project

1. Annotate layout on 200 pages chosen across centuries and parishes, margins and
   signatures included; train the detector; measure margin-line recall.
2. Check 50-100 pages of lines across as many hands as possible; fine-tune two CTC
   recognisers and one 2-4B line VLM; score each, and their lattice, against held-out
   lines, beside Haiku 5.5 on the same pages as the frontier reference.
3. Build the lattice and a simple adjudicator prompt with an off-the-shelf 7-9B model;
   measure how many spans are disputed and how often the adjudicator settles them
   correctly.
4. Decide from those three numbers (margin recall, lattice error, disputed share)
   whether to build the rest.
