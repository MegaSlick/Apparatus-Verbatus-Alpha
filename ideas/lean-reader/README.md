# A lean reader: shrinking the pipeline for self-hosting and scale

**ACTIVE IDEA. Research note, written 2026-10-14 by a Claude session while the lead
slept. Not a decision. Do not delete without the lead's approval.**

Nothing in this note has been measured on the registers. It draws on this repository,
published papers, model cards and vendor pages; every outside figure names its source in
[SOURCES.md](SOURCES.md), and figures from a single weak source are marked *unverified*.
The experiments that would settle each question are in [EXPERIMENTS.md](EXPERIMENTS.md).

## The answer in five lines

1. **Yes, it is worth pursuing, but not for the money.** Claude Haiku 5.5 now costs
   $0.10 / $0.50 per million tokens, a tenth of Haiku 4.5. A whole pass over 120k pages
   comes to roughly **$70 in batch mode**. No self-hosted setup beats that on cost alone.
   The case for a lean reader is **control**: pinned weights that never get retired,
   registers that never leave the lead's machines, no usage terms over what you build,
   and a reader that keeps working when a vendor changes its model.
2. **The thing to build is a small reader trained on these hands, not a smaller copy of
   a general model.** A 3-7B vision model or a 5-20M parameter line recogniser,
   fine-tuned on Quebec registers, has beaten frontier models on this kind of material
   in every published comparison found. A model like that is not made obsolete by the
   next frontier release.
3. **Spend the big reader only where the small ones disagree.** Most register lines are
   easy and every witness agrees on them. A cascade that sends the Perlector only the
   lines or pages where cheap readers disagree is the largest speed lever. The published
   printed-text version of this sends about 7% of inputs to the big model.
4. **A swarm helps only if its members fail differently.** Ten models sharing one
   language prior agree on the same wrong word. What adds evidence is different kinds of
   reader (line recognisers next to vision models) and different views of the image. The
   repository already measures this: the rescue rate and error correlation in
   `operations/bakeoff/roster.py`.
5. **Two things to settle before training:** (a) Anthropic's usage policy restricts
   training a model on Claude outputs without its prior authorisation, and its wording
   reaches transcriptions used as labels, not only reasoning, so the Haiku passes need
   a written answer before they become training targets; and (b) whatever is built must
   read the whole act, margins and godparents included, which rules out the
   field-extraction shortcuts the large Quebec indexing projects took (below).

**Recommended next step:** a two-day pilot that fine-tunes one line recogniser and one
3-4B vision model on pages the lead has checked, then scores both against the current
witnesses with the bake-off's own scorer. It costs a few tens of dollars of GPU time and
answers whether the rest is worth building ([EXPERIMENTS.md](EXPERIMENTS.md), E1 and E2).

## Decisions for the lead

Each comes with a recommendation.

| # | Decision | Recommendation |
|---|---|---|
| D1 | May the Haiku and "Luna" transcriptions be used as training labels? | **Ask Anthropic in writing first; it costs one email.** The lead's distinction is real: the model would learn to read the image, not to imitate Claude, and it never sees how or why a label was made. But Anthropic's Acceptable Use Policy is written about outputs, not reasoning: it lists "use outputs to train an AI model (e.g. model scraping or model distillation) without prior authorization from Anthropic", and a transcription used as a training target is the usual meaning of distillation in the papers (olmOCR was trained exactly that way, on GPT-4o's transcriptions). The policy offers prior authorisation, and a narrow, non-competing archival reader is the kind of case to ask about. Until then, use the Haiku outputs where no question arises: as an evaluation reference, and to choose which pages the lead checks by hand (pages where Haiku and the witnesses disagree are the most useful to check). A page the lead has checked is the lead's own label, whatever suggested it. Check "Luna"'s terms the same way (this session could not tell which model that is). Not legal advice. |
| D2 | Contact Teklia and the BALSAC project (UQAC)? | **No; treat their work as the baseline to beat, not a source.** The lead reports that they index the pages and share only names, and their own paper shows the gap: an extraction pipeline that keeps chosen fields, with only 74% of birth and death acts complete and valid. This project's aim is different: the full diplomatic reading of every act, with its margin notes, godparents and witnesses. Their published numbers are still useful as a yardstick (line error 6.3-6.9% on 1850-1920 hands after about 1,000 training pages). |
| D3 | Which target first: a Mac mini appliance, or a fast cluster run? | **Cluster first.** A rented GPU reads 120k pages with a fine-tuned 3B model in about a day for well under $100. A Mac mini reads roughly 100-150 pages an hour with a 4B model (*unverified*), so as a vision reader it suits a private, steady trickle of a few thousand pages a day, not a 120k-page backlog. Line recognisers change that: on its CPU a Mac mini reads every line of the backlog in hours, so a Mac can run Tier 1 for everything and a vision model only for the disputed part. The same trained weights serve both. |
| D4 | Keep the Perlector large? | **Yes, for now.** Shrink the witnesses and the share of the page the Perlector reads, not the Perlector itself. A small Perlector is a later experiment (E6) once the cascade shows how little it needs to read. |

## What changed, in numbers

| Way to read 120k pages once | Rough cost | Rough time | Source |
|---|---|---|---|
| Claude Haiku 4.5, batch | about $565 | hours | Anthropic pricing; 1,500 output tokens a page |
| Claude Haiku 5.5, batch | about $70 | hours | same |
| One self-hosted witness on a rented A40 | about $90 | about 9 days at 6.5 s a page | this repository's Chandra and Churro cards (5-8 s a page measured) |
| Chandra on one H100 | about $60 | about 23 hours at 1.44 pages a second | Datalab; printed pages |
| A fine-tuned 1-3B reader on one H100 | $25-50 | about 7-12 hours at 3-6 pages a second | LightOnOCR-2 and olmOCR-2 vendor figures; printed pages, so handwritten pages with long answers will be slower |
| A CTC line recogniser on one A40 | a few dollars | about 2 hours at about 600 lines a second | Kiessling 2026; segmentation not counted |
| PyLaia on CPU, no language model | electricity | about 30 CPU-hours (0.03 s a line, about 30 lines a page): a few hours across a Mac mini's cores | Teklia's PyLaia paper; segmentation not counted |
| A 4B vision model on a Mac mini (4-bit, MLX) | electricity | about 35-40 days at 25-30 s a page | community benchmark, *unverified* |

The lesson is that per-page reading is already cheap everywhere. What makes a real run
expensive is the 27B Perlector reading every page in full, three witnesses each reading
every page, the pod hours spent waiting, and the lead's own time. A lean pipeline should
attack those, in that order.

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
- **The same registers have been read before.** Teklia and BALSAC (UQAC) ran a
  non-LLM pipeline (Doc-UFCN line detection, PyLaia or Kaldi with a character 6-gram
  language model, NER, validation rules) over about two million BAnQ pages of
  1850-1920 Quebec registers. Its recogniser reached 6.3-6.9% character error on their
  test set after training on 1,077 pages (45,856 lines), and 74% of the birth and death
  acts it extracted were complete and valid. Their output is an index of chosen fields, not
  full readings: the lead reports they share only names, and margin notes and godparents
  are not what they kept. That is the baseline a lean reader has to beat on reading
  quality, and its hands are far later than the 1700s registers this project starts with. For the
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

## The design: a cascade, not a bigger committee

The pipeline's shape stays. What changes is how much each stage reads and how many
stages read everything. Every tier below writes evidence the Perlector can be shown;
none of them establishes text, which keeps ARCHITECTURE's claim and invariants intact.

```
Tier 0  every page, CPU, milliseconds      Ink map (exists), page classification, blank
                                           check, line and record detection (exists),
                                           image views: grey, contrast-stretched,
                                           binarised, per colour channel
Tier 1  every line, CPU or small GPU       two or three line recognisers fine-tuned on
                                           these registers, each on two image views;
                                           each line comes with per-character confidence
Tier 2  every page or record, one GPU      one small vision reader fine-tuned on these
                                           registers (3-4B, 8-bit), the "lean witness"
Agreement  model-free                      align every reading of each line; mark each
                                           span agreed or disputed, with the candidates
Tier 3  disputed spans only                the Perlector reads the page, shown the
                                           agreement map and the disputed lines' crops
Checks  every act, deterministic           act formula, date grammar, name lists: flags
                                           for review, never edits
```

### Tier 1: line recognisers as cheap witnesses

These are the fastest readers that exist for handwriting, and they fail differently from
vision-language models: a recogniser that cannot read a letter writes a wrong letter,
where a vision-language model writes a fluent wrong word. That difference is what makes
them useful next to the existing witnesses. The bake-off already has the arms (kraken,
PyLaia, PARTY, PP-OCRv6 through kraken) with their pretrained weights; what has not been
done is fine-tuning them on these registers. Their per-character posteriors are also
the best-calibrated confidence the pipeline could get, much better than a vision-language
model's token probabilities.

They are also fast enough for a Mac mini: PyLaia reads a line in about 0.03 s on one CPU
core (0.12 s with a language model), so every line of 120k pages is about 30 CPU-hours.
Their weakness is that they need line segmentation, and on CPU segmentation dominates
their time in this repository's own measurements (kraken's segmenter took about 100 s for
one synthetic page on 4 vCPU). Surya's lines, which the Designator already produces, are
the cheaper source.

### Margin notes and godparents: what a fast tier must not drop

The project wants every word of every act: the marginal name and act number, later
marginal annotations (a marriage or death noted beside a baptism years afterwards), the
godparents, witnesses and signatures. The cheap tiers are where those get lost: line
detectors trained on body text skip short marginal lines and signatures, record
detectors draw the box around the act body, and a cascade that only escalates *disputed*
text never escalates text no tier saw at all. So:

- Tier 0 must find margin and signature regions as well as body lines, and the ink map
  already holds a page when inked area is claimed by no reading region; that check must
  stay on every page whatever the cascade routes.
- The act-formula check should list the slots each act type carries (the godparents in
  a baptism, the witnesses in a marriage) and flag an act whose reading lacks one.
- The held-out set in E0 must include pages with marginal annotations, and E1-E5 report
  recall of margin lines and of godparent names separately, not only page CER, which
  barely moves when a three-word margin note is lost.

### Image views instead of more models

Reading the same line under several image treatments is cheaper than adding a model and
gives more independent evidence: in a study of handwritten death records, consensus over
ten perturbed views cut character error from 9.0% to 7.2%, where temperature sampling of
the same model helped less. Grid warp gave the least correlated errors. Colour-channel
views matter on registers with faded iron-gall ink, where one channel often holds the
contrast the others lose. Tier 0 makes the views once; Tier 1 reads two or three of them.

### The agreement map: lighter witnesses for the Perlector

Today the Perlector's prompt carries each witness's full page. With more, cheaper
witnesses that would grow, not shrink. Instead, align every reading of each line into one
compact record: the agreed text, and at each disputed span the candidates and who
proposed them. On an easy line that is just the line. This is the shape the Perlector
needs to spend its attention on the hard spans, and it is far fewer prompt tokens.

It must stay testimony: no vote picks a winner. The Perlector still reads the ink and the
agreement map is a clue. The trap generator (`operations/bakeoff/mutations.py`) already
plants errors that every witness agrees on, which is exactly the failure an agreement map
could hide; the "voting must lose" check is the guard.

### Tier 3: the Perlector reads less

With an agreement map, the Perlector is asked about the disputed spans and confirms the
rest from the image. Two forms, in order of risk:

- **Page-level routing:** pages where every line is agreed and the agreement is strong
  still go to the Perlector, but in a short form ("confirm or correct") with a small
  answer. Lowest risk, modest saving.
- **Line-level escalation:** the Perlector is shown the page and only the disputed lines'
  crops. Larger saving. It must keep the whole-page view, because acts are named from
  the whole page and a missed act is the worst failure.

Either way, a page the cascade marks easy is still read. Agreement is never treated as
truth: the unanimous-but-wrong rate is measured separately on names, dates and old
spellings, the cases where a shared French language prior makes every model "correct"
the ink the same way.

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
  into a date and checked against the page's neighbours. A date out of sequence is
  flagged, never changed (the Coniector already looks for this in text).
- **Name lists:** given names, surnames and places of the period (from public
  genealogical sources, licence permitting) to flag a name no list knows, and to give the
  agreement map a tie-breaking hint the Perlector may weigh. The danger is lexicon-
  constrained decoding: it raises scores by normalising rare spellings, which is the
  opposite of a diplomatic reading. If a recogniser is ever decoded with a lexicon, its
  unconstrained reading is kept beside it.
- **Act-type rules** like the ones Teklia and BALSAC used to reject invalid or merged
  records.

These are small programs, not models, and they run in milliseconds.

### A search index before a transcript

One idea from the large archive projects is worth its own line. The PRHLT group's
*probabilistic indexing* keeps every likely word a recogniser saw at each place, with its
probability, instead of committing to one transcription; Bentham, the Spanish notarial
archives and German parish registers were made searchable that way at a scale of
hundreds of thousands of images (on the Passau parish registers, structured table
queries reached 0.90 average precision from 200 training pages). For this project that
would mean a searchable index over all 120k pages, built by Tier 1 alone in days, long
before every act has an established reading. It would be a separate product beside the
Armarium, labelled as a search aid and never as text.

## Training: what, from what, and how

- **Base models:** churro-3B or a Qwen 3-4B vision model for the lean witness; kraken
  or PP-OCRv6 recognisers for Tier 1. Check licences first: churro's weights are under
  Qwen's research licence, which may not suit a published tool.
- **Labels, in order of trust:** the lead's checked gold; pages the lead corrects from
  the pipeline's own review (`verbatus decide` edits are exactly this); the Perlector's
  established readings where every check passed and the witnesses agreed; Teklia's
  RecordGold if its terms allow. Not the Haiku outputs unless D1 is settled.
- **Recipe:** Teklia's two stages: train on the large, noisier set, then polish on the
  small checked set. Keep doubt marks (`[[?]]`) in the targets so the model learns to say
  when it cannot read, as `operations/training/export.py` already does for the Perlector.
- **Held out:** a frozen set of checked pages the training never sees, the same one for
  every candidate.
- **Hardware:** a 3-4B LoRA fits a single 24 GB card; a run over tens of thousands of
  examples is hours and tens of dollars on a rented 4090 or 5090 (*estimate*). A
  recogniser fine-tune is minutes to an hour. Apple Silicon can fine-tune a 3B model
  with mlx-vlm, slowly; better as a place to run the result than to train it.

## Running it

- **Cluster:** vLLM with the lean witness at 8 bits and the line recognisers on the same
  card or on CPU. The Perlector on its own card, fed only what the cascade escalates.
  The queue runner and pod guard already handle the day.
- **Mac mini:** the line recognisers on the CPU (the whole backlog in hours), the lean
  witness at 8 bits through MLX on the pages or lines that need it, and the Perlector either at 4-8 bits on a 64 GB M5 Pro (slow) or not at all, in
  which case pages with disputes are held for a person or for a later GPU pass. The Mac's
  weak point is image prefill: a whole page is thousands of image tokens, so line crops
  and the agreement map suit it far better than whole-page vision reads. Prefer the M5
  Pro for its memory bandwidth (about 307 GB/s against about 160 GB/s for the M6;
  secondary sources).

## What this note would not do

- Train on the Haiku transcriptions before D1 is answered.
- Add many vision-language witnesses that share a language prior and call their
  agreement confidence.
- Let any period-knowledge model change a reading.
- Quantise below 8 bits without measuring character error on the held-out set first.
- Buy hardware before E1-E3 show what runs where.

## Unknowns this note could not resolve

- Which model "Luna" is, and its terms.
- Where the Haiku and "Luna" passes and their outputs live; they are not in this
  repository (perhaps pagekit, which this session cannot read).
- Every throughput figure for handwritten register pages: the published ones are for
  printed documents.
- Whether churro's research licence and Teklia's undeclared licence allow a released tool
  built on them.
