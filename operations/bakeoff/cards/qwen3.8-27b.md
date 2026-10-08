# Qwen3.8-27B run card

**Verdict in one line:** ready-untested-here (the plain arm ran on 2026-10-08; the vendor arm has not run on a GPU)

## Identity

- Repo `Qwen/Qwen3.8-27B`, revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` (the
  `[chairs.perlector]` pin in `config/models-real.toml`; `git ls-remote` on 2026-10-08
  still returns it as `refs/heads/main`).
- Licence Apache-2.0 (card metadata): use, modification and redistribution allowed with
  notice.
- 51.8 GiB on disk (bf16 safetensors). Family: VLM, `Qwen3_5ForConditionalGeneration`
  (hybrid linear/full attention, 48 of 64 layers linear), vision patch 16, merge 2.

## Install

Project venv, `uv sync --frozen --group pod` (vLLM 0.30.0, transformers 5.14.1,
qwen-vl-utils 0.0.14, the perlector row's `required_packages`). No extra install. The
vLLM recipe asks for vLLM >= 0.17.0 and transformers >= 5.8.0; vLLM 0.30.0's
`registry.py` lists `Qwen3_5ForConditionalGeneration` and its `reasoning/__init__.py`
registers the `qwen3` parser. Risk: none beyond the pod group's own.

## Native inference path

The vendor documents serving through vLLM or SGLang with an OpenAI client:

- Model card `README.md@1d4bf0f`, "Instruct (or Non-Thinking) Mode": one user turn,
  image part then text part, `temperature=0.7, top_p=0.8, presence_penalty=1.5`,
  `extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}}`, no
  `max_tokens` in that example.
- vLLM recipe `models/Qwen/Qwen3.8-27B.yaml@d80a169` (github.com/vllm-project/recipes):
  `vllm serve Qwen/Qwen3.8-27B --max-model-len 262144 --reasoning-parser qwen3`; thinking
  off per request (`chat_template_kwargs`) or server-wide with
  `--default-chat-template-kwargs '{"enable_thinking": false}'`.
- OCR wording: Qwen3-VL cookbook `cookbooks/ocr.ipynb@9658872` (github.com/QwenLM/Qwen3-VL),
  "Full Page OCR for Multilingual text". No Qwen3.8-specific OCR cookbook exists:
  github.com/QwenLM/Qwen3.8 resolves to the same repository as QwenLM/Qwen3.5 (head
  `2ea10dc`), which holds only a README and LICENSE.

Our `qwen-vendor` arm reproduces the model card's non-thinking request with the
cookbook's instruction.

## Prompt

Vendor (cookbook, verbatim): `Please output only the text content from the image without
any additional descriptions or formatting.` No system prompt (the cookbook comments it
out; the chat template adds none when thinking is off). The arm appends the project's
rules: keep spelling, accents, abbreviations, punctuation and capitalisation; one output
line per written line in reading order; `[[?]]` for unread ink
(`arms.QWEN_VENDOR_PROMPT`).

## Preprocessing

The checkpoint's `preprocessor_config.json@1d4bf0f`: `size.shortest_edge` 65,536 and
`size.longest_edge` 16,777,216 pixels (min/max pixels; 64 to 16,384 visual tokens at 32 px
per token), `Qwen2VLImageProcessorFast`, mean/std 0.5, colour (RGB). The cookbook's
document demo uses 512x32x32 to 2048x32x32 on Qwen's hosted API, a hosted-cost setting
rather than a model limit. No crop, no line segmentation: the whole page.

## Decoding

Card non-thinking preset: temperature 0.7, top_p 0.8, top_k 20, min_p 0.0,
presence_penalty 1.5, repetition_penalty 1.0. Thinking preset (not used): 1.0 / 0.95 /
20 / 0 / 0.0 / 1.0. `generation_config.json` ships 1.0 / 0.95 / 20 (the thinking
defaults). Output length: the card recommends up to 131,072 tokens for a final answer;
context 262,144 native. Stop: the model's EOS ids (248046, 248044).

## Output and normalisation

Plain text, one line per written line, sometimes inside a markdown code fence.
`score.normalise_output` falls through to its generic branch (drops `<think>` blocks and
markdown table rules); no arm-specific branch is needed.

## Resources

bf16 weights 51.7 GiB: the 96 GB card (`generic-80gb-plus`; the 24 and 48 GB rows are
`unsupported`). A full page at the vendor's 16.7 Mpx bound is up to about 16,400 image
tokens, so pass `--max-num-batched-tokens 16384`. Seconds per page: not measured; an
estimate of 1-3 minutes single-stream for 1,500-4,000 output tokens, much less amortised
at 32 sequences.

## Known failure modes

- Thinking left on: the template opens with `<think>`; without `--reasoning-parser` the
  reasoning lands in `content`. The arm sends `enable_thinking: false`, which makes the
  template emit an empty `<think></think>`.
- presence_penalty 1.5 penalises every token already written; on a register that repeats
  names, dates and formulae the card warns of "language mixing and a slight decrease in
  performance". It may push the model to alter repeated spellings.
- Sampling at 0.7 makes runs vary; the arm fixes a per-request seed (0).
- No handwriting guidance from the vendor; benchmarks are printed-document OCR
  (OmniDocBench, CC-OCR, OCRBench).

## What differs from our vLLM adapter

Against `qwen-blind` and the perlector serving row:

- Prompt: blind uses the project's own instruction; vendor uses the cookbook sentence
  plus the same rules.
- Sampling: blind is greedy (0.0 / 1.0 / top_k 0, no penalties); vendor is the card's
  non-thinking row (equal to `[chair_decoding.perlector]` in `config/decoding.toml`).
- Pixels: the row's `max_pixels` 5,299,200 (about 5,200 tokens); vendor 16,777,216.
- Reply length: blind sends 12,288 when it fits; vendor sends no cap (the context's
  remainder, 65,536 minus the prompt).
- Same: thinking off, image before text, no system prompt, `--generation-config vllm`,
  no `--reasoning-parser` (not needed with thinking off), prefix caching off (hybrid
  model), `max_model_len` 65,536 (the vendor's 262,144 is not needed for one page).

## Untested here

No GPU here. The tests run the arm against `fake_vllm_server.py`. First command on the pod:

```sh
.venv/bin/python -m operations.bakeoff.witness_run run --model qwen-vendor \
  --label qwen38-27b-vendor --repo Qwen/Qwen3.8-27B \
  --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 --weights <snapshot> \
  --max-num-batched-tokens 16384 --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache
```

## Our arm

`operations/bakeoff/arms.py`, arm `qwen-vendor` (family preset `qwen3.8`), run by
`witness_run.py`; cache label `qwen38-27b-vendor`. The plain arm is `qwen-blind`, label
as run on 2026-10-08.

## Sources

- https://huggingface.co/Qwen/Qwen3.8-27B/blob/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0/README.md
  (and `preprocessor_config.json`, `generation_config.json`, `chat_template.jinja`, `config.json` at that revision)
- https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/ocr.ipynb
- https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md (instruct eval hyperparameters, pixel control)
- https://github.com/vllm-project/recipes/blob/d80a1692a40e79bc6a5dc77e5a2d71c32e3d4b37/models/Qwen/Qwen3.8-27B.yaml
- https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/model_executor/models/registry.py

## Phase R baseline

Use the **vendor** arm. It is what the pipeline already sends the Perlector (the sealed
non-thinking sampling row) and what the vendor documents, so Phase R's changes are
measured against the production decoding rather than against greedy. Keep `qwen-blind`
as the greedy control: if greedy reads better on the bake-off pages (presence penalty
hurting repeated names), that is a finding for the lead, not a reason to change the
baseline silently. The vendor arm needs one run on the 96 GB card first.
