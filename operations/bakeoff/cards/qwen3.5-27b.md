# Qwen3.5-27B run card

**Verdict in one line:** ready-untested-here (the plain arm has run on a GPU; the vendor arm has not)

## Identity

- Repo `Qwen/Qwen3.5-27B`, revision `fc05daec18b0a78c049392ed2e771dde82bdf654`.
  Not in `config/models-real.toml`; pass it with `--repo/--revision`.
- Licence Apache-2.0 (card metadata): use, modification and redistribution allowed with
  notice.
- 51.8 GiB on disk (bf16 safetensors). Family: VLM, `Qwen3_5ForConditionalGeneration`
  (the same architecture as Qwen3.8-27B), vision patch 16, merge 2.

## Install

Project venv, `uv sync --frozen --group pod` (vLLM 0.30.0, transformers 5.14.1). The
vLLM recipe asks for vLLM >= 0.17.0; 0.30.0 registers the architecture and the `qwen3`
reasoning parser. Risk: none beyond the pod group's own.

## Native inference path

- Model card `README.md@fc05dae`, "Instruct (or Non-Thinking) Mode": one user turn,
  image then text, `max_tokens=32768, temperature=0.7, top_p=0.8, presence_penalty=1.5`,
  `extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}}`.
  Serving: `vllm serve Qwen/Qwen3.5-27B --tensor-parallel-size 8 --max-model-len 262144
  --reasoning-parser qwen3`.
- vLLM recipe `models/Qwen/Qwen3.5-27B.yaml@d80a169`: same command; "Disable reasoning:
  add `--default-chat-template-kwargs '{"enable_thinking": false}'`"; Mamba prefix
  caching "experimental".
- OCR wording: Qwen3-VL cookbook `cookbooks/ocr.ipynb@9658872`. github.com/QwenLM/Qwen3.5
  (head `2ea10dc`) holds only a README and LICENSE: no Qwen3.5 OCR cookbook.

Our `qwen-vendor` arm reproduces the card's non-thinking request with the cookbook's
instruction.

## Prompt

Vendor (cookbook, verbatim): `Please output only the text content from the image without
any additional descriptions or formatting.` No system prompt. The arm appends the
project's verbatim rules (`arms.QWEN_VENDOR_PROMPT`).

## Preprocessing

`preprocessor_config.json@fc05dae`: min 65,536 and max 16,777,216 pixels, RGB, mean/std
0.5, the whole page; identical to Qwen3.8-27B.

## Decoding

Card non-thinking preset for general tasks: temperature 0.7, top_p 0.8, top_k 20, min_p
0.0, presence_penalty 1.5, repetition_penalty 1.0. The card gives a second non-thinking
row "for reasoning tasks" in two different versions (1.0 / 0.95 / 20 / pp 1.5 in the
quick-start note; 1.0 / 1.0 / 40 / pp 2.0 in Best Practices); neither applies to OCR.
`generation_config.json` ships 0.6 / 0.95 / 20. Output: 32,768 tokens recommended for
most queries; context 262,144 native, at least 128K advised only to keep thinking
working.

## Output and normalisation

Plain text, one line per written line; the generic branch of `score.normalise_output`.

## Resources

bf16 51.7 GiB: the 96 GB card (`generic-80gb-plus`). At most 16,384 image tokens per
page at the vendor's bound (16,777,216 px / 32²); pass `--max-num-batched-tokens 16384`. Seconds per page: not
measured; same estimate as Qwen3.8-27B (same architecture and size).

## Known failure modes

As Qwen3.8-27B: thinking must be off; presence_penalty 1.5 against repeated names and
formulae; sampled output varies (seed fixed at 0). The CUDA graph / Mamba cache size
error on start is fixed by lowering `--max-cudagraph-capture-size` (recipe).

## What differs from our vLLM adapter

Against `qwen-blind` and the perlector row: prompt (cookbook sentence plus our rules),
sampling (card non-thinking row instead of greedy), `max_pixels` 16,777,216 instead of
5,299,200, no reply cap instead of 12,288. Same: thinking off, image first, no system
prompt, no reasoning parser, prefix caching off, `max_model_len` 65,536.

## Untested here

No GPU here. First command on the pod:

```sh
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-vendor \
  --label qwen35-27b-vendor --repo Qwen/Qwen3.5-27B \
  --revision fc05daec18b0a78c049392ed2e771dde82bdf654 --weights <snapshot> \
  --max-num-batched-tokens 16384 --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

## Our arm

`operations/bakeoff/arms.py`, arm `qwen-vendor` (family preset `qwen3.5`); cache label
`qwen35-27b-vendor`.

## Sources

- https://huggingface.co/Qwen/Qwen3.5-27B/blob/fc05daec18b0a78c049392ed2e771dde82bdf654/README.md
  (and `preprocessor_config.json`, `generation_config.json`, `chat_template.jinja` at that revision)
- https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb
- https://github.com/vllm-project/recipes/blob/d80a1692a40e79bc6a5dc77e5a2d71c32e3d4b37/models/Qwen/Qwen3.5-27B.yaml
- https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/model_executor/models/registry.py

## Phase R baseline

Use the **vendor** arm, for the same reason as Qwen3.8-27B: its sampling equals the
Perlector's sealed row, so the comparison with the incumbent is like for like, and the
plain arm stays as the greedy control.
