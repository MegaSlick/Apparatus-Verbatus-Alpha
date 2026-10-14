# Experiments that would settle the lean-reader questions

**ACTIVE IDEA. Proposals only; nothing here has been run. Do not delete without the
lead's approval.**

Each experiment uses instruments the repository already has wherever it can, names what
it costs, and says in advance what result would stop the idea. Every one is scored on
the same frozen held-out pages, checked by the lead, that no training ever sees. GPU
time is the lead's decision (AGENTS.md); the figures below are estimates to decide
with, not approvals.

Order matters: E0 and E1 come first and are cheap. E2 waits for a training set; E3 and
E5 change nothing until the pipeline has produced a real export. For the clean-sheet
design, its own "first week" in [CLEAN-SHEET.md](CLEAN-SHEET.md) replaces this list.

Every experiment chooses on a **development** set and reports once on a separate
**test** set that nothing was tuned on. Pages already used to pick bake-off prompts and
rosters count as development.

## E0. Freeze the yardstick (no GPU)

- Pick 150-300 checked pages across the page groups `operations/bakeoff/groups.py`
  scores (acts by century, index and list, tables, blank-like), plus the hard pages.
- Include pages with marginal annotations, signatures and later marginal notes, and
  mark in the gold which lines are marginal and which words are godparents' and
  witnesses' names, so every experiment can report their recall apart from page CER.
- Freeze them as the held-out list `operations/training/export.py --held-out` reads, and
  as the bake-off gold.
- Count the checked pages and the hands they cover; that number decides how much
  training is possible at all.
- Score the current witnesses, the Perlector and the Haiku passes already made on them
  (the bake-off scorer reads any cached answers). These are the numbers to beat, and
  the first measurement of Haiku on these registers.
- **Stop if:** fewer than about 150 checked pages exist. Then the first job is checking
  pages, not training.

## E1. Fine-tune line recognisers on these registers

- Train kraken (from McCATMuS) and PyLaia (from a Teklia French model) on Surya's lines
  from the checked pages that are not held out: first 30, then 100 pages, to see the
  curve. Two image views each: the render and a contrast-stretched grey.
- Score with the existing line arms (`operations/bakeoff/lines/`) and `score.py`. Read
  `roster.py`'s rescue rate and error correlation against Chandra, DAI and Churro.
- Cost: minutes to an hour of GPU per model, or a night on the Mac's CPU.
- **Go if:** either recogniser comes within a few points of the best witness on the
  `acts` group without losing margin lines, or has a high rescue rate with low error correlation (it catches what
  the others miss). **Stop if:** it is far behind and its errors track the others'.

## E2. Fine-tune one small vision reader ("the lean witness")

- Base: a Qwen 3-4B vision model (Apache-2.0); churro-3B only as a research comparison,
  since its weights are under a research licence.
  LoRA, Teklia's two stages: a larger, noisier set first, then the checked pages. No
  Perlector silver set exists until the pipeline has accepted real pages, and silver
  chosen because the witnesses agreed teaches agreement, so keep it a minority.
- Serve at bf16 and at 8 bits; score both with `witness_run` and `score.py`.
- Cost: one rented 24-48 GB card for a few hours; estimate $10-40.
- **Go if:** at 8 bits it matches or beats the best current witness on `acts`, and its
  answers are healthy (no loops, few empty pages). **Stop if:** it is worse than churro-3B
  untuned.

## E3. The agreement map and its risk-coverage curve (no GPU)

- From the cached witness answers (and E1/E2's, once they exist), align each line's
  readings and compute per-span agreement.
- Plot, against the held-out gold, the error rate of agreed spans as the agreement
  threshold tightens (a risk-coverage curve), separately for names, dates, numbers and
  ordinary words.
- Measure the unanimous-but-wrong rate on the same split. The trap generator's
  `plant-3` scenario gives a synthetic version of the same failure.
- **Go if:** some threshold leaves most lines "agreed" with an error rate close to the
  Perlector's own on those lines. **Stop if:** unanimous errors on names and dates are
  common; then agreement cannot route anything.

## E4. Image views

- Run the best Tier 1 recogniser and the lean witness on four views per page (render,
  grey contrast-stretched, Sauvola binarised, best colour channel) and on a padded or
  grid-warped copy.
- Score each view alone and the per-character vote across views.
- **Go if:** the vote beats the best single view by a margin larger than the noise floor
  (run one view twice to see it). Cheap: CPU for the views, the same GPU as E1/E2.

## E5. The Perlector reads less

- With `fed_arm.py`, send the Perlector two new feeds built from the same sealed run:
  (a) the agreement map in place of the full witness pages, and (b) the page plus only
  the disputed lines' crops.
- Score with `fed_score.py --compare` against the run's own reading: CER, act recall,
  scepticism, and the planted-error block under `mutations.py` traps.
- Measure prompt tokens and seconds per page for each.
- **Go if:** act recall, margin-line recall and godparent-name recall do not drop and CER stays inside the noise floor while prompt
  tokens fall substantially. **Stop if:** act recall drops at all. A lost act is the one
  failure the project will not trade for speed.

## E6. A smaller Perlector (later)

- Only after E5. Train a 7-9B Perlector with the existing exporter
  (`operations/training/`), whose trap mix already teaches scepticism, and judge it in
  the chair with `fed_arm` and the blind baseline ARCHITECTURE requires: a candidate
  whose advantage disappears once the witnesses are taken away has not learned to read.

## E7. A Mac mini dry run

- After E1-E2: the recognisers on CPU and the lean witness at 8 bits through MLX, on 500
  pages. Measure pages an hour, peak memory and watts.
- Decide D3 with those numbers, not with published ones.

## E8. Throughput on a cluster

- One day on one rented card: Tier 0-2 over a few thousand pages, then the Perlector on
  what E3's threshold escalates. Report pages an hour per tier and the share escalated.
  The queue runner already records wall time and throughput per arm.
