# Surya OCR recogniser run card
**Verdict in one line:** ready-untested-here, with a caveat for the lead: in surya-ocr
0.22.1 the recogniser is a vision-language model, not a CTC line model, so this arm does
not belong with the CTC lines

## Identity
- Code: `surya-ocr==0.22.1` (Apache-2.0), the repository's Surya environment
  (`operations/serving/surya/`, unchanged).
- Weights: `datalab-to/surya-ocr-2` (Hub commit `3b3d4cdf88d6928b0acdc75181b13206ea67c4a3`,
  `model.safetensors` 1,372,368,672 bytes, SHA-256 in Sources) and the GGUF build the arm uses on the CPU, `datalab-to/surya-ocr-2-gguf` at
  `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`: `surya-2.gguf` 1,266,400,864 bytes, SHA-256
  `1f18abe17b1ed8b4e47ee9b1ad0e274c93daf5efbb6b29a04ff1712e37051e05`;
  `surya-2-mmproj.gguf` 204,986,688 bytes, SHA-256
  `98c0563673b1657ff6d021d1e5f04af06cbf61bb40c63ac613e8bb71b42fb2c0`.
- Family: VLM, 650M parameters (`Qwen3_5ForConditionalGeneration`). It can invent text.
- Licence: "AI PUBS OPEN RAIL-M LICENSE (MODIFIED)", Version 0.1, March 2, 2023
  (Modified). Attachment A, "Use restrictions", item 2, quoted exactly:
  > 2. Commercial:
  > (a) for any purpose if You (your employer, or the entity you are affiliated with)
  > generated more than five million US Dollars ($5,000,000) in gross revenue in the prior
  > year, except where Your Use is limited to personal use or research purposes;
  > (b) for any purpose if You (your employer, or the entity you are affiliated with) has
  > raised more than five million US dollars ($5,000,000) in total equity or debt funding
  > from any source, except where Your Use is limited to personal use or research
  > purposes; or
  > (c) for any purpose if You (your employer, or the entity you are affiliated with)
  > provides or otherwise makes available any product or service that competes with any
  > product or service offered by or made available by Licensor or any of its affiliates.

  The model card summarises it as "free for research, personal use, and startups under
  $5M funding/revenue". Reading: a non-commercial research project under both $5M
  thresholds may use it under (a) and (b), which in any case exempt research use; (c) has
  no research exception and turns on whether the project "provides or makes available"
  a product or service competing with Datalab's (an OCR service). A bake-off that only
  measures is research use; publishing a transcription pipeline built on these weights is
  the lead's call. The restrictions also bind the model's output ("Output" is in the
  "Use" definition). The weights' licence applies to the GGUF build too (same LICENSE).

## Install
- The repository's Surya environment: `python -m operations.bakeoff.lines.surya_rec install`
  runs `uv sync --frozen --project operations/serving/surya` (the committed lock). 8 s
  here with torch already in uv's cache; a few minutes cold.
- The pod runs the GPU backend: `--serve` starts vLLM from the project environment (the
  locked vLLM 0.30.0, whose registry has `Qwen3_5ForConditionalGeneration`, the same
  architecture the bake-off already serves for Chandra) on the Hub checkpoint at the
  pinned commit, served as `datalab-to/surya-ocr-2`, with the witness runner's own
  launcher (`witness_run.Server`: start, `/v1/models` health check, stop). Surya's own
  vLLM backend starts a Docker image, which a RunPod pod cannot do, so the arm starts the
  server itself and hands Surya its URL; the server stops when the pages are done.
  `fetch --serve --store-root STORE` puts the checkpoint in `STORE/hf/surya-ocr-2` (no
  token; about 1.4 GB) and checks `model.safetensors` against its SHA-256.
- The CPU backend (a Mac) needs llama.cpp's `llama-server` on PATH
  (`brew install llama.cpp`); not in any lock. `--server-url` attaches to any running
  OpenAI-compatible server serving the model under that name.

## Native inference path
- `surya/recognition/__init__.py`@0.22.1 `RecognitionPredictor.__call__` and
  `_full_page_ocr`; `surya/inference/__init__.py` `SuryaInferenceManager`;
  `surya/inference/backends/llamacpp.py`, `vllm.py`, `spawn.py`; `surya/inference/prompts.py`.
- It takes page images and, for block mode, `LayoutResult`s (layout blocks, not text
  lines). There is no line recogniser in 0.22.1.
- Our worker (`surya_rec.py worker`, run with the Surya environment's interpreter) calls
  `RecognitionPredictor(manager)([image], full_page=True)` (`--mode page`, Surya's default
  and "more accurate path") or `([image], [layout], full_page=False)` with the runner's
  cached fast-layout blocks (`--mode blocks`).

## Prompt
- page: "OCR this image to HTML. Each block is a div with data-label and data-bbox (x0 y0
  x1 y1, normalized 0-1000)."
- blocks: "OCR this block image to HTML."

## Preprocessing
`Image.open(page).convert("RGB")` (Surya's own loader); block mode crops each block with
4 px padding (`_crop_block`). Detections come from the cached Surya runner documents.

## Decoding
Greedy (temperature 0), one request per page or per block, logprobs on; page mode
`SURYA_MAX_TOKENS_FULL_PAGE` 12,288, one greedy pass, then block mode for a page that
loops or fails to parse. Block mode's budget is the block's `count` + 100 tokens; the
fast-layout blocks the runner caches carry `count` 0, so each block gets **100 tokens**,
which truncates any paragraph. Use `--mode page` unless the lead wants the detections.
llama.cpp: 8 parallel slots.

## Output and normalisation
Block HTML in reading order (`reading_order`); skipped visual blocks contribute nothing.
Plain lines: block and break tags end a line, table cells are spaced
(`score._strip_html`), NFC, whitespace collapsed.

## Resources
- The plan's 15-minute estimate: the plan is not in this repository; its basis not found.
  Datalab's figure is 5 pages/s on an RTX 5090 (vLLM, full page).
- GPU: a 650M VLM, about 3-4 GB of weights in bf16 (1.4 GB file); fits any card. The
  arm asks vLLM for 0.92 of the card and a 32,768-token context. Not measured; page mode
  sends one page at a time, a few seconds a page on an A40 by estimate, so the 20-minute
  box includes the server's start.
- CPU (llama.cpp, GGUF): not measured; a 650M VLM writing about 1,000 tokens a page at a
  few tens of tokens a second a slot: estimate 0.5-2 min a page on a pod CPU, faster on a
  Mac with Metal.

## Known failure modes
Generative: loops (Surya retries page mode in block mode), invented text, HTML markup;
block mode truncation (above); trained on modern documents and 91 languages, not on
19th-century registers.

## What differs from our vLLM adapter
no vLLM adapter exists in this repository for Surya's OCR model

## Untested here
The real model on either backend, and vLLM 0.30.0 actually loading this checkpoint (the
architecture is registered; a 650M Qwen3.5 has not been served here). Tested here: the
`--serve` start, health check and stop against the fake vLLM server. Tested here: the worker under the real Surya 0.22.1
environment against a stand-in OpenAI server (both modes, the runner-shaped layout
accepted by `LayoutResult`). The Mac runs first:
`python -m operations.bakeoff.lines.surya_rec install && python -m operations.bakeoff.lines.surya_rec check && which llama-server`

## Our arm
- Module `operations.bakeoff.lines.surya_rec`; arm `surya-rec-surya` (`--mode page`, the
  default; reads no cached detections) and `surya-rec-surya-blocks` (`--mode blocks`, the
  cached layout blocks; its own cache folder, so the two modes never mix on resume).
- On the pod (the queue's `surya-rec-surya`): `python -m operations.bakeoff.lines.surya_rec
  fetch --serve --store-root STORE`, then `python -m operations.bakeoff.lines.surya_rec run
  --serve --pages DIR --out CACHE --store-root STORE` (vLLM on port 8191, started and
  stopped by the run; its log in `CACHE/surya-rec-surya/server.log`).
- On a Mac: `fetch --store-root STORE` (the GGUF pair), then `run --pages DIR --out CACHE
  --store-root STORE [--server-url http://127.0.0.1:8000/v1] [--mode blocks --lines-dir
  SURYA_DOCS]`.

## Sources
- surya-ocr 0.22.1 wheel from PyPI (files above); `surya/settings.py`
- https://huggingface.co/datalab-to/surya-ocr-2 at `3b3d4cdf88d6928b0acdc75181b13206ea67c4a3`
  (`LICENSE`, `README.md`, `config.json`); `model.safetensors` LFS SHA-256
  `5755f82a997dd0b111964fa8b31cc2daef7aeb7a706bbd17d73d6a93ef3f723e`
- https://huggingface.co/datalab-to/surya-ocr-2-gguf at `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`
