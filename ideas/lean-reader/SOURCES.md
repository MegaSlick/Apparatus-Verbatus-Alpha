# Sources for the lean-reader note

**ACTIVE IDEA. Do not delete without the lead's approval.**

Gathered by web research on 2026-10-14. Search tools often returned summaries rather than
whole pages; a source marked *summary* was not read in full, and a figure resting on one
such source is marked *unverified* in the note. Check a figure at its source before
anyone repeats it publicly.

## Prices

- Anthropic pricing (Haiku 5.5 $0.10 / $0.50 per million tokens up to 100k-token
  prompts; Haiku 4.5 $1 / $5; batch 50% off): https://platform.claude.com/docs/en/about-claude/pricing
- Image tokens (about w/28 x h/28; up to about 4,784 tokens a page on the high-resolution
  tier): https://platform.claude.com/docs/en/build-with-claude/vision


## Small readers on historical handwriting

- Teklia DAI reader card (44.1% / 14.9% / 9.2% CER; Gemini 3.0 Pro 16.3%):
  https://huggingface.co/Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR
- CHURRO paper (3B, 99,491 pages; handwritten French 81.1 vs Gemini 2.5 Pro 74.5;
  hallucination analysis): https://arxiv.org/abs/2509.19768
- Kiessling, CTC and PP-OCRv6 vs a 9B line VLM on CATMuS and ICDAR 2026 medieval tasks;
  few-shot curves; about 600 lines a second on an A40 (read in full):
  https://arxiv.org/pdf/2609.20064
- Gemini Pro Vision 56% CER vs CNN-BiLSTM 9.8% on French historical handwriting:
  https://arxiv.org/html/2410.24034v1
- OmniHandwritingOCR (Qwen3-VL-8B best general MLLM; handwriting "far from solved";
  *summary*): https://arxiv.org/pdf/2608.18586
- "Reading or Guessing?" (fluent errors from VLMs; *summary*): https://arxiv.org/abs/2605.27750
- Chandra 2 card: https://huggingface.co/datalab-to/chandra-ocr-2
- LightOnOCR-2 card and paper (throughput on H100): https://huggingface.co/lightonai/LightOnOCR-2-1B,
  https://arxiv.org/pdf/2601.14251
- olmOCR (distilled from GPT-4o labels) and olmOCR 2 (RL with unit-test rewards):
  https://arxiv.org/pdf/2502.18443v3, https://arxiv.org/pdf/2510.19817

## Line recognisers, combination, indexing

- PyLaia paper (0.01 s a line on GPU, 0.03 s on CPU, 0.12 s with a language model;
  6-gram LM gains; confidence calibration): https://arxiv.org/pdf/2404.18722
- Teklia and BALSAC, Quebec parish records (about 2M pages, 6.3-6.9% CER, 74% complete
  and valid acts, 1,077 training pages): https://arxiv.org/abs/2304.14044,
  https://www.teklia.com/research/projects/balsac/details
- Riksarkivet SATRN (30-100 pages to adapt to a new period; *Swedish*):
  https://huggingface.co/Riksarkivet/satrn_htr
- McCATMuS kraken model: https://zenodo.org/records/13788177
- PARTY: https://github.com/mittagessen/party, https://zenodo.org/records/15764161
- Loghi (no Mac support): https://github.com/knaw-huc/loghi
- ROVER with naive transcribers (*summary*): https://dl.gi.de/items/b69cde8a-6891-47aa-a5e4-5608a4696ff5/full
- HTR and ASR confusion-network combination: https://doc.rero.ch/record/311026/files/138_2009_Article_208.pdf
- TrOCR ensemble vote on Latin (1.86% to 1.60%): https://arxiv.org/pdf/2508.11499
- Probabilistic indexing (Toselli, Puigcerver, Vidal 2024): https://link.springer.com/book/10.1007/978-3-031-55389-9;
  Passau parish registers: https://iapr.org/archives/icfhr2018/SlidesPosters/Slides-Paper115.pdf
- Nouvelle-France numérique (*summary*): https://crkn-rcdr.ca/fr/conference/conference-virtuelle-du-rcdr/fr-program/transcrire-les-documents-manuscrits-laide-de
- PRDH record counts: https://www.erudit.org/en/revue/haf/2001/v55/n2/010400ar.pdf

## Confidence, cascades, test-time views

- Consensus Entropy (CVPR 2026; routing about 7.3% of inputs to a stronger VLM; printed
  OCR): https://arxiv.org/html/2504.11101v4
- Beyond Logprobs (logprobs weak; fused signals strong; invoice fields):
  https://arxiv.org/pdf/2606.24420
- Test-time image augmentation on handwritten death records (9.0% to 7.2% CER;
  unanimous-but-wrong cases): https://arxiv.org/html/2509.09722
- Geometric Risk Controller (*abstract only*): https://arxiv.org/abs/2603.19790
- HIPE-OCRepair 2026 (over-correction on clean input; printed text):
  https://arxiv.org/pdf/2607.08143
- No Free Lunches, LLM post-correction (open models made historical Finnish worse):
  https://arxiv.org/html/2502.01205v1
- FrugalGPT: https://arxiv.org/abs/2305.05176

## Quantisation, fine-tuning, hardware

- OCR loss under 4-bit: https://arxiv.org/pdf/2508.03351, https://arxiv.org/pdf/2602.24059,
  https://arxiv.org/pdf/2412.19509; vision tower vs language model:
  https://arxiv.org/pdf/2601.09555
- Qwen's GGUF builds with a 16-bit vision projector: https://huggingface.co/Qwen/Qwen3-VL-2B-Thinking-GGUF
- Unsloth vision fine-tuning: https://unsloth.ai/docs/basics/vision-fine-tuning
- EAGLE draft head for Chandra (1.79x single-stream; community): https://huggingface.co/jbarrow/joecr-chandra-2-eagle3.1
- Mac mini M6 and M5 Pro line-up (secondary): https://daringfireball.net/2026/08/configurations_and_pricing_for_new_mac_minis_and_mac_studios,
  https://www.apple.com/shop/buy-mac/mac-mini
- Community MLX benchmarks (4B 4-bit on M4 Pro: about 76 tokens a second;
  Qwen2.5-VL-7B on M4: about 12-13; *unverified*): https://omlx.ai/benchmarks

## From this repository

- Witness throughput on an A40 (5-8 s a page): `operations/bakeoff/cards/chandra.md`,
  `operations/bakeoff/cards/churro.md`
- Rescue rate and error correlation: `operations/bakeoff/roster.py`
- Trap generator and "voting must lose": `operations/bakeoff/mutations.py`
- Perlector feed variants and scorecard: `operations/bakeoff/fed_arm.py`,
  `operations/bakeoff/fed_score.py`
- Training exporter: `operations/training/export.py`
- FP8 and MTP serving rows: `config/serving_recipes_real_variants.toml`
