# A lean reader: shrinking the pipeline for self-hosting and scale

**ACTIVE IDEA. Research note, written 2026-10-14 by a Claude session while the lead
slept, revised the same night after an adversarial review. Not a decision. Do not
delete without the lead's approval.**

Nothing in this note has been measured on the registers. It draws on this repository,
published papers, model cards and vendor pages; every outside figure names its source in
[SOURCES.md](SOURCES.md), and figures from a single weak source are marked *unverified*.
The experiments that would settle each question are in [EXPERIMENTS.md](EXPERIMENTS.md).

**Two documents, two framings.** This page asks how the existing pipeline could shrink
and stays within its contracts. [CLEAN-SHEET.md](CLEAN-SHEET.md) is the fresh-eyes
answer the lead asked for: what to build from scratch, possibly as a new project,
ignoring how the current pipeline is built. **Read that one first.**

## The answer in five lines

1. **Yes, it is worth pursuing, but not for the money.** Claude Haiku 5.5 now costs
   $0.10 / $0.50 per million tokens, a tenth of Haiku 4.5: a whole pass over 120k pages
   is roughly **$70 in batch mode**. No self-hosted setup beats that on cost. The case
   for a lean reader is **control**: weights pinned by digest that are never retired,
   registers that never leave the lead's machines, no usage terms over what is built,
   and a reader that keeps working when a vendor changes its model.
2. **The thing to build is a reader trained on these hands, not a smaller copy of a
   general model.** A 3-7B vision model or a 5-20M parameter line recogniser, fine-tuned
   on the registers, has beaten frontier models on comparable material in every
   published comparison found. A model like that is not made obsolete by the next
   frontier release.
3. **The cheapest, safest first piece is line recognisers as extra witnesses.** They are
   tiny, run on a CPU, learn a new hand from tens of pages, and fail differently from
   the vision-language witnesses (a wrong letter, not a fluent wrong word). That last
   property is the one that matters, and `operations/bakeoff/roster.py` already measures
   it (rescue rate and error correlation).
4. **A swarm helps only if its members fail differently.** Ten models sharing one French
   language prior agree on the same wrong word. Different kinds of reader and different
   views of the image add evidence; more of the same does not. Making the Perlector read
   less, by routing on witness agreement, is the tempting next step and the riskiest: it
   changes the architecture and should wait for a real export.
5. **Two constraints shape everything:** (a) Anthropic's usage policy restricts training
   a model on Claude outputs without its prior authorisation; the lead reads it as not
   covering this project, and a short written confirmation would settle it (D1); and (b) the goal is the whole act, margin notes, godparents
   and witnesses included, which rules out the field-extraction shortcuts the large
   Quebec indexing projects took.

**Recommended next step, in order:**

1. **No GPU:** count the pages the lead has checked, and score the Haiku passes already
   made against them beside the three witnesses (the bake-off scorer reads any cached
   answers). That gives the first real number for Haiku on these registers, and says
   whether there is enough checked material to train anything (E0).
2. **Cheap:** fine-tune two line recognisers on those pages and score them as witnesses
   (E1). Minutes to an hour of GPU, or a night on a Mac's CPU.
3. **Wait:** nothing that changes what the Perlector is shown until the pipeline has
   produced one real export. A small vision reader (E2) waits for a training set that
   does not yet exist.

## Decisions for the lead

Each comes with a recommendation.

| # | Decision | Recommendation |
|---|---|---|
| D1 | May the Haiku and "Luna" transcriptions be used as training labels? | **The lead's position:** the project trains a model to read images, never to imitate Claude, and builds nothing that competes with it. **This note's one flag:** the Acceptable Use Policy's wording ("use outputs to train an AI model ... without prior authorization from Anthropic") is about outputs as such, so a short written confirmation from Anthropic would remove any doubt before the Haiku transcriptions become training targets. The lead decides; nothing in the experiments depends on it, since checked pages come first anyway. Not legal advice. |
| D2 | Use Teklia and BALSAC's work? | **As a yardstick only.** The lead reports that they index the pages and share only names, and their own paper shows the gap: an extraction pipeline that keeps chosen fields, with 74% of birth and death acts complete and valid. This project wants the full diplomatic reading of every act. Their line error (6.3-6.9% on 1850-1920 hands after about 1,000 training pages) is the number to compare against. |
| D3 | Which target first: a Mac mini or a cluster? | **Neither yet; decide from E1 and E7.** On a rented card a fine-tuned 3B vision reader is not a one-day job on handwriting: churro-3B measures 5-8 s a page on an A40 here, so 120k pages is about 9 A40-days or roughly 3 H100-days. A Mac mini reads roughly 100-150 pages an hour with a 4B vision model (*unverified*), fine for a steady few thousand pages a day, not a 120k backlog. Line recognisers are fast enough for a Mac (about 30 CPU-hours for every line of 120k pages), but only once lines are found, and line finding on a Mac's CPU is unmeasured and may cost far more than the reading. |
| D4 | Keep the Perlector large? | **Yes.** Shrink the witnesses first. A smaller Perlector, or one that reads less, comes after a real export (E5, E6). |
| D5 | Seat Haiku 5.5 as a witness? | **Score it first (step 1), then decide.** At about $70 a pass it is cheaper than any self-hosted witness, and registers going to the lead's own Claude account is allowed (AGENTS.md). Against it: it cannot be pinned by digest, it will be retired one day, and runs made with it cannot be repeated byte for byte. As one witness among several, whose testimony is kept and never final, that may be acceptable; as the Perlector it would not be. |
| D6 | Is a search index over unread pages in scope? | **Later, and the lead's call.** See *A search index before a transcript*: useful, but a second surface beside the established reading, which PRINCIPLES' "same reading everywhere" has to be squared with. |

## What it costs today, and what changed

| Way to read 120k pages once | Rough cost | Rough time | Source |
|---|---|---|---|
| The three current witnesses on rented A40s | about $250-300 | about 650 A40-hours (19.5 s a page for three) | this repository's Chandra and Churro cards (5-8 s a page measured); DAI assumed similar; A40 at about $0.40 an hour, *check* |
| The Perlector (27B) | **unmeasured**, probably the largest single part | unmeasured | the card's estimate is 1-3 minutes a page single-stream, much less batched |
| Claude Haiku 4.5, batch | about $565-700 | hours | Anthropic pricing; 1,500 output tokens a page; Haiku 4.5 sees a smaller image (at most about 1,600 tokens), which gives the lower figure |
| Claude Haiku 5.5, batch | about $70 | hours | same, with the image at up to about 4,800 tokens |
| A fine-tuned 3B vision reader | about $90 on A40s, or $150-250 on H100s | about 9 A40-days or 3 H100-days | churro-3B measured here; the vendor figures of 3-6 pages a second are for printed pages with short answers |
| A CTC line recogniser on one A40 | a few dollars | about 2 hours at about 600 lines a second | Kiessling 2026; line finding not counted |
| PyLaia on CPU, no language model | electricity | about 30 CPU-hours (0.03 s a line, 30 lines a page) | Teklia's PyLaia paper; line finding not counted |
| A 4B vision model on a Mac mini (4-bit, MLX) | electricity | about 35-40 days at 25-30 s a page | community benchmark, *unverified* |

Two lessons. Per-page reading by any one model is cheap everywhere; what makes a run
expensive is the number of readers on every page, the 27B Perlector, pod hours spent
waiting, and the lead's own time. And the first number to measure is the one missing
from this table: what the Perlector costs per page on a real run.

## What is already known about small readers on this material

- **Teklia's DAI reader, already witness 2** (Qwen2.5-VL-7B with a LoRA on French parish
  records of the 16th-18th centuries): 44.1% character error zero-shot, 14.9% after
  about 7k gold records, **9.2%** after first training on 30k noisy crowd records and
  then the gold. Gemini 3.0 Pro scored 16.3% on the same test. Noisy data first and a
  small clean set last is the recipe to copy.
- **Churro (3B)**, trained on about 100k historical pages: on handwritten French it
  scores 81.1 normalised similarity against Gemini 2.5 Pro's 74.5 and its own base
  model's 59.2, and its error analysis found no major hallucinations against 36% for the
  untuned base.
- **Line recognisers learn a new hand from very little.** Kraken's 5M-parameter model
  improves on its pretrained one from about 20 annotated lines, and after fine-tuning a
  16M-parameter PP-OCRv6 recogniser (3.4% on medieval French) beat a 9B vision model
  trained for the same task (3.7%), while running about 45 times faster on CPU. That was
  medieval material, not 18th-century cursive.
- **Quebec registers have been read at scale, but not like this.** Teklia and BALSAC
  (UQAC) ran a non-LLM pipeline (Doc-UFCN line detection, PyLaia or Kaldi with a
  character 6-gram language model, NER, validation rules) over about two million BAnQ
  pages of 1850-1920 registers: 6.3-6.9% character error after training on 1,077 pages
  (45,856 lines), and 74% of the birth and death acts complete and valid. Their output
  is an index of chosen fields, not full readings with margins and godparents. For the
  French regime, UQAR and BAnQ's *Nouvelle-France numérique* has Transkribus models for
  17th-18th century sources (no error rates published).
- **How many pages a new hand needs:** Riksarkivet reports that 30-100 corrected pages
  halve the error of its Swedish line model on an unseen period; BALSAC used about 1,000.
- **Zero-shot general models lose to small tuned ones on historical handwriting.** An
  older study had Gemini Pro Vision at 56% character error on French historical
  handwriting where a small CNN-BiLSTM trained on 500 pages reached 9.8%. The gap is
  smaller with current models but has not closed in any comparison found.
- **Quantisation:** 8-bit (FP8 or Q8) is close to lossless on vision-language
  benchmarks. 4-bit costs several to 20 points on OCR benchmarks, worst on 2-3B models.
  Keep the vision tower at 16 bits; that is how Qwen's own GGUF builds ship it.

## The real bottleneck: hands and checked pages

120k pages over two centuries and many parishes hold hundreds of hands, and a hand
changes with each priest. "30-100 pages per new hand" across hundreds of hands is more
checking than one person does, so the questions that decide whether training pays are:
how many checked pages exist now, how many hands they cover, and how fast the lead can
check more. Three ways to stretch that budget:

- Train one model across hands and polish it on a spread of checked pages, as Teklia
  did, rather than one model per hand.
- Check the pages that teach the most: where the witnesses, the recognisers and Haiku
  disagree, and hands with no checked page yet.
- Count corrections made in `verbatus decide` reviews as checked material; they are the
  lead's own readings, made in the course of work that has to happen anyway.

## The design, in the order it could be built

The pipeline's shape stays. Every addition below writes testimony or checks; none of it
establishes text.

```
Now     Tier 0  every page, CPU     Ink map (exists), line and record detection (exists),
                                    margin and signature regions, image views (grey,
                                    contrast-stretched, binarised, per colour channel)
Now     Tier 1  every line          two line recognisers fine-tuned on these registers,
                                    seated as witnesses, with per-character confidence
Now     Checks  every act           act formula, date grammar, name lists: flags for
                                    review, never edits
Later   Tier 2  every page          one small vision reader trained on these registers
Later?  Routing                     the Perlector reads less where readers agree:
                                    an architecture change, the lead's decision
```

### Tier 1: line recognisers as cheap witnesses

These are the fastest readers there are for handwriting, and they fail differently from
vision-language models: a recogniser that cannot read a letter writes a wrong letter,
where a vision-language model writes a fluent wrong word. The bake-off already has the
arms (kraken, PyLaia, PARTY, PP-OCRv6 through kraken) with pretrained weights; what has
not been done is fine-tuning them on these registers.

Open questions the experiments must answer, not assume:

- **Do they count toward the witness floor?** A line reader is not a page reader.
  Seating one means deciding how its lines become witness units the Perlector cites,
  and whether it counts toward `witness_floor = 3`. That is a contract change
  (`pipeline/3_attestatores/CONTRACT.md`).
- **Line finding.** They need lines. Surya's, which the Designator already produces on
  the GPU, are the natural source; on a Mac's CPU, line finding is unmeasured, and
  kraken's segmenter took about 100 s for one synthetic page on 4 vCPU here, which for
  120k pages is months of one machine.
- **Confidence.** Their per-character scores are reasonably calibrated on hands like the
  training set's and can be confidently wrong on a new scribe, which is where they would
  matter most. Calibrate per hand, or do not route on them.

### Margin notes and godparents: what a fast tier must not drop

The project wants every word of every act: the marginal name and act number, later
marginal annotations (a marriage or death noted beside a baptism years afterwards), the
godparents, witnesses and signatures. The cheap tiers are where those get lost: line
detectors trained on body text skip short marginal lines and signatures, and record
detectors draw the box around the act body. So:

- Tier 0 must find margin and signature regions as well as body lines. The ink-map check
  that holds a page when inked area is claimed by no reading region stays on every page.
- The act-formula check lists the slots each act type carries (godparents in a baptism,
  witnesses in a marriage) and flags an act whose reading lacks one.
- The held-out pages include marginal annotations, and every experiment reports recall
  of margin lines and of godparent names apart from page CER, which barely moves when a
  three-word margin note is lost.

### Image views instead of more models

Reading the same line under several image treatments is cheaper than adding a model and
can give more independent evidence: in a study of handwritten death records, consensus
over ten perturbed views cut character error from 9.0% to 7.2%, where temperature
sampling of the same model helped less. Grid warp gave the least correlated errors.
Colour-channel views may matter on faded iron-gall ink. No study found measured this on
line recognisers; E4 does.

### Specialist checks, not specialist correctors

The lead's idea of readers that know the period and the region is sound as a **check**
and wrong as a **corrector**. ARCHITECTURE says the Perlector writes what the ink shows
even when a date looks wrong, and PRINCIPLES judges accuracy against the page, not
against what a plausible entry of the period would say. A historical model that fixes
readings would break both. As flags for review they are cheap and useful:

- **Act formula:** a baptism, marriage or burial follows a known formula (*L'an mil sept
  cent... a été baptisé...*). A deterministic template check says which slots an act has
  and which are missing, a strong signal for a merged or truncated act.
- **Date grammar:** dates written in words, with month abbreviations like *7bre*, parsed
  and checked against the page's neighbours. A date out of sequence is flagged, never
  changed (the Coniector already looks for this in text).
- **Name lists:** given names, surnames and places of the period (licence permitting) to
  flag a name no list knows. The danger is lexicon-constrained decoding: it raises scores
  by normalising rare spellings, the opposite of a diplomatic reading. If a recogniser is
  ever decoded with a lexicon, its unconstrained reading is kept beside it.
- **Act-type rules** like the ones Teklia and BALSAC used to reject invalid or merged
  records.

These are small programs, not models, and run in milliseconds.

### Later, and only by the lead's decision: the Perlector reads less

The largest saving on paper is to show the Perlector less: replace the witnesses' full
pages with one aligned record per line (agreed text, and the candidates at each
disputed span), or ask only about the disputed lines. Published printed-OCR work routes
about 7% of inputs to the big model. This note does not recommend it yet, for four
reasons:

- **It is a vote by another name.** An "agreed text" is a consensus output, and showing
  it as the default makes agreement the fact-finder's prior. ARCHITECTURE says the
  witnesses are never options to choose between, and invariant 9 says a witness's
  reading is never itself an output. Any version must keep the raw testimonia beside the
  map, which gives back much of the saving.
- **It is new architecture.** Today only witnesses are routed, from Designator evidence,
  before any witness reads. Letting testimony decide how the Perlector reads a page
  changes the Perlector's contract and the page accounting (every witness unit cited or
  set aside). That is the lead's decision.
- **The saving is smaller than it looks.** The Perlector must keep the whole page,
  because acts are named from the whole page and a lost act is the worst failure. Its
  cost is mostly the image and a long structured answer, not the witness text, so
  trimming the witnesses saves a share of the prompt, not 93% of the calls.
- **Unanimous errors.** Every reader with a French language prior "corrects" the same
  old spelling, name or date the same way. The trap generator's `plant-3` scenario and
  the "voting must lose" check exist for exactly this, and E3 measures it on real pages
  before anything routes.

### A search index before a transcript

The PRHLT group's *probabilistic indexing* keeps every likely word a recogniser saw at
each place, with its probability, instead of committing to one transcription; Bentham,
Spanish notarial archives and German parish registers were made searchable that way at
hundreds of thousands of images (on the Passau parish registers, structured queries
reached 0.90 average precision from 200 training pages). Here it would mean a searchable
index over all 120k pages from Tier 1 alone, long before every act has an established
reading. It is a second surface beside the established reading, so it is a scope
decision for the lead (D6), labelled as a search aid and never as text.

## Training: what, from what, and how

- **Base models:** kraken or PP-OCRv6 recognisers first. For a vision reader, a Qwen 3-4B
  vision model (Apache-2.0) rather than churro-3B, whose weights are under Qwen's
  research licence, or DAI, which declares no licence at all, so deriving from it is not
  permitted by default.
- **Labels, in order of trust:** the lead's checked pages; the lead's corrections from
  `verbatus decide`; and only then machine readings. Two cautions on machine readings:
  the pipeline has no accepted real page yet, so a Perlector "silver" set does not exist;
  and choosing silver pages *because the witnesses agreed* trains a model to agree with
  them, the exact failure ARCHITECTURE warns about for the Perlector. Silver chosen that
  way should be a minority of the data and never the polish stage. Not the Haiku outputs
  unless D1 is settled.
- **Recipe:** Teklia's two stages: train on the large, noisier set, then polish on the
  checked set. Keep doubt marks (`[[?]]`) in the targets so the model learns to say when
  it cannot read, as `operations/training/export.py` already does for the Perlector.
- **Two held-out sets, not one.** A development set to choose models, thresholds and
  feeds, and a test set touched once at the end. Pages already used to pick bake-off
  prompts and rosters belong to development.
- **Hardware:** a recogniser fine-tune is minutes to an hour. A 3-4B LoRA fits a single
  24 GB card; tens of thousands of examples is hours and tens of dollars on a rented
  4090 or 5090 (*estimate*). Apple Silicon can fine-tune a 3B model with mlx-vlm,
  slowly; better as a place to run the result than to train it.

## Running it

- **Cluster:** what exists today, plus the recognisers on the same card or on CPU beside
  the witnesses. The queue runner and pod guard already handle the day.
- **Mac mini:** the recognisers on the CPU once lines exist, and a small vision reader
  at 8 bits through MLX for a steady trickle of pages. The Perlector at 4-8 bits on a
  64 GB M5 Pro would be slow; pages could instead wait for a GPU pass. The Mac's weak
  point is image prefill (a whole page is thousands of image tokens), so line crops suit
  it better than whole-page reads. Prefer the M5 Pro for memory bandwidth (about 307 GB/s
  against about 160 GB/s for the M6; secondary sources).

## What this note would not do

- Add many vision-language witnesses that share a language prior and call their
  agreement confidence.
- Train on silver pages chosen because the witnesses agreed, and polish on them.
- Let any period-knowledge model change a reading.
- Change what the Perlector is shown before a real export exists.
- Quantise below 8 bits without measuring character error on held-out pages first.
- Buy hardware before E1 and E7 show what runs where.

## Unknowns this note could not resolve

- Which model "Luna" is, and its terms.
- Where the Haiku and "Luna" passes and their outputs live; they are not in this
  repository (perhaps pagekit, which this session cannot read).
- How many pages the lead has checked, and how many hands they cover.
- What the Perlector costs per page on a real run.
- Every throughput figure for handwritten register pages: the published ones are for
  printed documents.
