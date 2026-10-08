# Chandra OCR 2 run card

**Verdict in one line:** ready-untested-here (the package's own pipeline runs end to end against the fake server on CPU; no GPU run yet)

## Identity

- Repo `datalab-to/chandra-ocr-2`, revision `af93b47dba1b47b6640c86ccf487ed2260ab9a09` (the repository's pin, `config/models-real.toml` `[chairs.attestator_1]`).
- Code: `chandra-ocr` 0.2.0, github.com/datalab-to/chandra at `d4f7467435aa4137d9539f000ddf0b7ced3eb43f`. That commit and the weights revision were both made the same day ("Adjust scores" / "Adjust scoring 85.9 -> 85.8"), so they are the matching pair. Tag `v0.2.0` is the earlier `497dfd3`; between the two only `chandra/model/hf.py` and the benchmark scripts changed, not the vLLM path.
- Licence: weights under a modified OpenRAIL-M (revenue-capped: organisations over $2M revenue or funding barred; competitive use barred; use restrictions apply). Accepted under the research track. Code Apache-2.0.
- Size on disk: 10.6 GB (one bf16 safetensors file). Family: VLM (Qwen3.5 architecture, `Qwen3_5ForConditionalGeneration`, hybrid attention).

## Install

- Own venv for the client: `operations/bakeoff/native/venvs/chandra-native/pyproject.toml` with `uv.lock` (produced here with `uv lock`, linux x86_64). It pins `chandra-ocr @ git+https://github.com/datalab-to/chandra@d4f7467…` and resolves openai 3.26.1, beautifulsoup4 4.15.0, markdownify 1.1.0, pydantic-settings 2.15.0, pillow 12.3.0. No torch: the package is only a client on its vLLM path.
- The vLLM server runs from the project pod venv (vLLM 0.30.0, transformers 5.14.1).
- Vendor's own pins (`pyproject.toml` at the commit): `beautifulsoup4>=4.14.2, click>=8, filetype>=1.2, markdownify==1.1.0, openai>=2.2.0, pillow>=10.2, pydantic>=2.12, pydantic-settings>=2.11, pypdfium2>=4.30, python-dotenv>=1.1.1, six>=1.17`; extra `hf`: `torch>=2.8, torchvision>=0.23, transformers>=5.2.0, accelerate>=1.11`. Its launcher runs the Docker image `vllm/vllm-openai:v0.17.0`.
- Install: `.venv/bin/python -m operations.bakeoff.native.chandra_native install --venv-dir /workspace/venvs/chandra_native` (about a minute; 37 packages and one git fetch).
- Risk: the git fetch of the package needs GitHub at install time (once).

## Native inference path

Read at `d4f7467`: `chandra/scripts/cli.py::main` (what `chandra INPUT OUTPUT --method vllm` does), `chandra/input.py::load_image`, `chandra/model/__init__.py::InferenceManager.generate`, `chandra/model/vllm.py::generate_vllm`, `chandra/model/util.py::scale_to_fit` and `detect_repeat_token`, `chandra/output.py::parse_markdown`/`parse_html`/`parse_layout`, `chandra/prompts.py`, `chandra/settings.py`, `chandra/scripts/vllm.py` (the server launcher).

Our arm calls the package itself, per page: `load_image(path)` → `InferenceManager("vllm").generate([BatchInputItem(image, prompt_type="ocr_layout")], max_output_tokens=12384, max_retries=6, include_images=True, include_headers_footers=False, vllm_api_base=…)`, exactly the CLI's per-page call, 28 pages in flight (the CLI's vLLM batch of 28 with one worker each). The server is the launcher's command on the project's vLLM:

```
vllm serve <snapshot> --host 127.0.0.1 --port 8191 --no-enforce-eager --max-num-seqs 32 \
  --dtype bfloat16 --max-model-len 18000 --max-num-batched-tokens 4096 \
  --gpu-memory-utilization .85 --enable-prefix-caching \
  --mm-processor-kwargs '{"min_pixels": 3136, "max_pixels": 6291456}' --served-model-name chandra
```

(`--max-num-seqs`/`--max-num-batched-tokens` by the launcher's own scaling from an 80 GB H100: 48 GB → 32 / 4096, 24 GB → 16 / 2048; `--vram-gb` picks the row. The launcher has no A40 row; 48 GB is its L40S row.)

## Prompt

The package's `OCR_LAYOUT_PROMPT` (`chandra/prompts.py`), one user turn, image first, then the prompt; no system turn. It is byte-identical to `common.chandra_layout.OCR_LAYOUT_PROMPT` (SHA-256 `025935f3e1de1acdfadd4c7d581ab17eb82e8caaffef7b64962621c80b7ca9a8`), so it is not repeated here. It opens: "OCR this image to HTML, arranged as layout blocks. Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format. Bboxes are normalized 0-1000. …"

## Preprocessing

- `load_image`: open, convert to RGB, and if either side is under 1,536 px (`MIN_IMAGE_DIM`) upscale so the short side is 1,536 (LANCZOS, `int()` truncation).
- `scale_to_fit`: fit within 3072 x 2048 = 6.29 M pixels on a 28 px grid (LANCZOS), sent as PNG. vLLM then applies its own patch-16 x merge-2 grid within 3,136 to 6,291,456 pixels.
- Whole page, colour (RGB), no crop, no line segmentation.
- The CLI's file finder accepts `.tiff` but not `.tif`; the arm passes each page path straight to `load_image`, which reads either.

## Decoding

- First request: temperature 0.0, top_p 0.1, `max_tokens` 12,384, nothing else sent (vLLM defaults; the model's generation_config has only the EOS id). Thinking: the model's chat template always closes an empty `<think></think>` after the assistant tag, so thinking is off whatever is sent.
- Retry loop (`generate_vllm::process_item`): if the answer's tail repeats (`detect_repeat_token`, also checked 50 characters from the end) or the request failed, ask again, up to 6 more times, at temperature min(0.2 n, 0.8) and top_p 0.95, sleeping 2 n s after an error. The last attempt is returned whatever it holds.
- Inside each attempt the package's OpenAI client (openai 3.26.1, its defaults: `max_retries=2`, 600 s timeout) itself re-sends a request that fails to connect, times out or gets a 408/409/429/5xx, so one attempt can be up to three HTTP requests. `--request-timeout` does not apply to this arm; the client's 600 s does.
- Concurrency: 28 pages in flight; the server takes 32 sequences.

## Output and normalisation

- Raw: top-level `<div data-bbox="x0 y0 x1 y1" data-label="…">` layout blocks of HTML (bboxes 0-1000). Cached as `raw_response`, with the package's `markdown`, the same with headers and footers kept, and each block's label and bbox in the loaded image's pixels (`parse_chunks`).
- The package's markdown (`parse_markdown`): drops `Blank-Page`, and by the CLI default drops `Page-Header`/`Page-Footer` blocks; wraps bare `Text` blocks in `<p>`; removes `<img>` without `src` in non-image blocks; markdownify 1.1.0 with ATX headings, `-` bullets, escaped `*`, `_` and `$`, math as `$…$`, and tables left as HTML.
- Our rule (`chandra_native.markdown_lines`): each table row becomes a line (cells joined by a space); `<br>` breaks a line; images (`![alt](src)`) dropped; link text kept; heading marks, `-` bullets, rules, `*`/`~~`/`$` markup removed; the escapes undone; other tags stripped, entities unescaped; one line per markdown line, empty lines dropped. The prompt asks for paragraphs joined with `<p>`, so a "line" is often a paragraph, not a written line.
- `--include-headers-footers` keeps page headers and footers in `text`; the cache has both versions either way.

## Resources

- VRAM: weights 10.6 GB; fits a 24 GB card (the repository measured startup on an RTX 4090; the launcher has a 24 GB row). The A40 48 GB gives the launcher's 32-sequence row.
- CPU: one thread per page in flight for image preparation (PIL); light.
- Seconds per page: about 5-8 s/page throughput at high concurrency and about 2 min median latency per page, by the first A40 run of our `chandra` arm (2026-10-08). The native arm adds the retry loop: a page that loops costs up to 7 x 12,384 tokens, so expect a longer tail than our arm. Datalab reports 1.44 pages/s on an H100 at 96 sequences (60 s mean, 156 s p95 latency) on olmOCR-bench pages.

## Known failure modes

- Loops on some spreads (seen in our arm); the vendor's retries change the temperature and usually end the loop, at a time cost.
- Context: the package always sends 12,384 as `max_tokens` against an 18,000-token context. A page at the `scale_to_fit` cap is about 6,144 image tokens plus about 900 prompt tokens, so prompt plus bound is about 19.4 k. Whether vLLM 0.30 refuses such a request (`vllm/renderers/params.py` checks prompt length against context minus `max_tokens`) or counts the image placeholder as one token at that check and lets generation stop at the context end was not determined here. If it refuses, every large page fails after seven attempts and is cached with an error.
- The package returns no `finish_reason`; the arm infers `length` when `token_count >= max_output_tokens`.
- The package prints errors to stdout and keeps no request log; the arm caches the final answer only, not the discarded attempts. `final_answer_repeats` applies the package's own retry test (repeat at the end, or 50 characters before the end) to that final answer: true means the retries ran out on a loop.

## What differs from our vLLM adapter

| | our `chandra` arm | `chandra-native` |
|---|---|---|
| Prompt | same bytes | same bytes |
| Pixels | `scale_to_fit` only | `load_image` first upscales pages whose short side is under 1,536 px, then `scale_to_fit` (same for register-size scans) |
| Tokens | 12,384 only when it surely fits, else omitted (answer to the context's end) | 12,384 always |
| Retries | first attempt only | up to 6 retries on a repeated tail or an error, temperature +0.2 each, top_p 0.95 |
| Thinking | `enable_thinking: false` sent | not sent; the chat template closes thinking anyway |
| Server | sealed serving row: no prefix caching, `--generation-config vllm`, `--max-num-seqs 32`, `--gpu-memory-utilization 0.92`, `--chat-template-content-format openai` | launcher's flags: prefix caching on, vLLM defaults otherwise, `.85` memory, 32 sequences at 48 GB |
| Post-processing | `common/chandra_layout.py` reader: every block kept (headers, footers, Blank-Page), tables as rows | package's `parse_markdown`: headers, footers and Blank-Page dropped by default; tables as rows via our `markdown_lines` |
| Tables | rows as lines | rows as lines (from the package's HTML-in-markdown) |

## Untested here

No GPU here: the real model and the real vLLM server have not run. Tested on CPU: the arm with the real `chandra-ocr` package (installed from the lock into a scratch venv) against `fake_vllm_server.py`, and with a stand-in package in the project tests. First command on the pod:

```
/workspace/venvs/chandra_native/bin/python -m operations.bakeoff.native.chandra_native run \
  --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store
```

then read `server.log` for a context-length refusal and each page's `units[0].error`.

## Our arm

- Module `operations/bakeoff/native/chandra_native.py`, arm name `chandra-native`, cache label `chandra-native`.
- `check`, `install --venv-dir DIR`, `fetch --store-root DIR`, `prepare` (nothing to prepare), `run …` as in the contract; extra flags `--vram-gb`, `--max-model-len`, `--max-output-tokens`, `--max-retries`, `--include-headers-footers`.
- No telemetry: nothing in `chandra/` at the commit sends anything except the OpenAI client to `VLLM_API_BASE` (searched for telemetry, analytics, posthog, sentry, requests, urllib, sockets). `chandra.settings` reads a `local.env` if one is found; there is no `CHANDRA_*` or datalab telemetry switch because there is no telemetry. The arm sets `HF_HUB_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1` for itself and the server.

## Sources

- github.com/datalab-to/chandra @ `d4f7467435aa4137d9539f000ddf0b7ced3eb43f`: `pyproject.toml`, `README.md` (Throughput), `chandra/settings.py`, `chandra/prompts.py`, `chandra/input.py`, `chandra/output.py`, `chandra/model/__init__.py`, `chandra/model/vllm.py`, `chandra/model/hf.py`, `chandra/model/util.py`, `chandra/model/schema.py`, `chandra/scripts/cli.py`, `chandra/scripts/vllm.py`; tags `v0.2.0` = `497dfd38d7625259307c81c979d44093e9f75f44`.
- huggingface.co/datalab-to/chandra-ocr-2 @ `af93b47dba1b47b6640c86ccf487ed2260ab9a09`: `chat_template.jinja`, `config.json`, `generation_config.json`, `preprocessor_config.json`, `LICENSE`, file listing (sizes); commit history of `main`.
- vllm-project/vllm @ `v0.30.0`: `vllm/renderers/params.py`, `vllm/entrypoints/serve/utils/api_utils.py`.
- This repository: `common/chandra_layout.py`, `common/chandra_presentation.py`, `common/imaging_ports.py`, `operations/bakeoff/arms.py`, `config/serving_recipes_real.toml`, `config/decoding.toml`.
