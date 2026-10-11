# Qwen3.5-9B run card

**Verdict in one line:** ready-untested-here (the plain arm has run on a GPU; the vendor arm has not)

## Identity

- Repo `Qwen/Qwen3.5-9B`, revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`.
  Not in `config/models-real.toml`; pass it with `--repo/--revision`.
- Licence Apache-2.0 (card metadata).
- 18.0 GiB on disk (bf16). Family: VLM, `Qwen3_5ForConditionalGeneration`, vision patch
  16, merge 2. No `generation_config.json` in the repository at this revision.

## Install

Project venv, `uv sync --frozen --group pod`. vLLM recipe: vLLM >= 0.17.0. Risk: none
beyond the pod group's own.

## Native inference path

- Model card `README.md@c202236`, "Instruct (or Non-Thinking) Mode": identical to the
  Qwen3.5-27B card (image then text, `max_tokens=32768`, 0.7 / 0.8 / top_k 20 /
  presence 1.5, `chat_template_kwargs: {"enable_thinking": False}`). Serving:
  `vllm serve Qwen/Qwen3.5-9B --tensor-parallel-size 1 --max-model-len 262144
  --reasoning-parser qwen3`.
- vLLM recipe `models/Qwen/Qwen3.5-9B.yaml@d80a169`: same; thinking off server-wide with
  `--default-chat-template-kwargs '{"enable_thinking": false}'`.
- OCR wording: Qwen3-VL cookbook `cookbooks/ocr.ipynb@9658872`.

## Prompt

Vendor (cookbook, verbatim): `Please output only the text content from the image without
any additional descriptions or formatting.` No system prompt. The arm appends the
project's verbatim rules (`arms.QWEN_VENDOR_PROMPT`).

## Preprocessing

`preprocessor_config.json@c202236`: min 65,536 and max 16,777,216 pixels, RGB, the whole
page; identical to the 27B models.

## Decoding

Card non-thinking preset: 0.7 / 0.8 / top_k 20 / min_p 0 / presence 1.5 / repetition
1.0 (the same two inconsistent "reasoning" rows as the 27B card, not used). Output 32,768
recommended; context 262,144 native.

## Output and normalisation

Plain text; the generic branch of `score.normalise_output`.

## Resources

bf16 about 18 GiB: would fit a 48 GB card (and probably a 24 GB one with a short context),
but the perlector chair has a vLLM row only at `generic-80gb-plus`, so the arm runs on
the 96 GB card unless a 48 GB row is added. Seconds per page: not measured; expect about
a third of the 27B's time.

## Known failure modes

As the 27B models, and likely a weaker reading of dense or faded hands than the 27B
(the cards report only printed-document benchmarks: OmniDocBench 1.5, CC-OCR, OCRBench;
no handwriting benchmark).

## What differs from our vLLM adapter

Same differences as Qwen3.5-27B: prompt, card non-thinking sampling instead of greedy,
`max_pixels` 16,777,216 instead of 5,299,200, no reply cap instead of 12,288. The row's
`max_model_len` 65,536 and 80 GB tier are kept.

## Untested here

No GPU here. First command on the pod:

```sh
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-vendor \
  --label qwen35-9b-vendor --repo Qwen/Qwen3.5-9B \
  --revision c202236235762e1c871ad0ccb60c8ee5ba337b9a --weights <snapshot> \
  --max-num-batched-tokens 16384 --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

## Our arm

`operations/bakeoff/arms.py`, arm `qwen-vendor` (family preset `qwen3.5`); cache label
`qwen35-9b-vendor`.

## Sources

- https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/README.md
  (and `preprocessor_config.json`, `chat_template.jinja`, `config.json` at that revision)
- https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb
- https://github.com/vllm-project/recipes/blob/d80a1692a40e79bc6a5dc77e5a2d71c32e3d4b37/models/Qwen/Qwen3.5-9B.yaml

## Phase R baseline

Use the **vendor** arm, as for the other two readers: the same sampling as the sealed
Perlector row keeps the three readers comparable; the plain arm is the greedy control.
