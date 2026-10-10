# Churro-3B run card

**Verdict in one line:** ready-untested-here (the release runner's settings and the repository's own text extractor run against the fake server on CPU; no GPU run yet)

## Identity

- Repo `stanford-oval/churro-3B`, revision `ca2150ea465d5a3d67818c50e234b9422619c75d` (the repository's pin, `config/models-real.toml` `[chairs.attestator_3]`; weights uploaded on the Hugging Face side in September 2025, last card edit at this revision).
- Runner: github.com/stanford-oval/Churro at `c8b2f28834991ebc96faef31562662bddc8062ee`, the last commit before the pinned weights revision; against the paper release `ed09bc7fd6475c333a25427f3d0b9227af46ce27`, `run_churro_ocr.py`, `utils/llm/models.py`, `utils/llm/core.py` and `evaluation/xml_utils.py` are unchanged; the benchmark harness classes were refactored and `encode_image` learned WEBP. The later package `churro-ocr` v0.3.0 (`4abb17386d9656199c2776195926545fc527a691`) is what our `churro` arm follows; its system string is the same text the runner sends by default.
- Licence: weights under the Qwen Research Licence (research use only); code Apache-2.0. Accepted under the research track.
- Size on disk: 7.5 GB (two bf16 safetensors). Family: VLM (fine-tuned `Qwen/Qwen2.5-VL-3B-Instruct`).

## Install

- Own venv for the client: `operations/bakeoff/native/venvs/churro-native/pyproject.toml` with `uv.lock` (produced here, linux x86_64): `lxml==6.1.3`, `pillow==12.3.0`, `rapidfuzz==3.14.5`, `huggingface_hub==1.31.0`, 19 packages in all. lxml is needed for the repository's own extractor (its recovering parser).
- The vLLM server runs from the project pod venv (vLLM 0.30.0).
- Vendor's own pins (`pixi.toml` at the runner commit): python 3.12, `litellm==1.76.1`, `lxml` (any), `xmlschema>=4.0.1,<5`, `Pillow` (any), `docker` (any); the server is the Docker image `vllm/vllm-openai:v0.10.2`, into which the runner also pip-installs `open_clip_torch` and `flash-attn`.
- Install: `.venv/bin/python -m operations.bakeoff.native.churro_native install --venv-dir /workspace/venvs/churro_native` (under a minute).
- Risk: low; lxml ships manylinux wheels.

## Native inference path

Read at `c8b2f28`: `run_churro_ocr.py` (`main`, `run_vllm_inference`, `load_image`, `trim_leading_prompt`, the `--prompt` and `--max-concurrency` defaults), `utils/docker/vllm.py::maybe_start_vllm_server_for_engine`, `utils/docker/servers.py::start_vllm_server`, `utils/llm/models.py` (`MODEL_MAP["churro"]`), `utils/llm/core.py::_run_litellm`, `utils/llm/messages.py::prepare_messages` and `encode_image`, `utils/llm/config.py`, `utils/utils.py::resize_image_to_fit`, `evaluation/xml_utils.py::extract_actual_text_from_xml`, `evaluation/historical_doc.xsd`.

The runner is `python run_churro_ocr.py --engine churro --image-dir DIR --output-dir OUT` (64 requests in flight). Our arm reproduces its server and its requests:

```
vllm serve <snapshot> --host 127.0.0.1 --port 8193 --gpu-memory-utilization 0.9 \
  --data-parallel-size 1 --tensor-parallel-size 1 \
  --max-model-len 20000 --served-model-name churro
```

(`--max-model-len 20000` is the `churro` row's `max_completion_tokens`, which `maybe_start_vllm_server_for_engine` passes as the context length. The vendor also passes `--trust_remote_code`; the snapshot has no Python files, so it runs nothing there, and the arm leaves it out so that only dots.mocr ever runs code from a snapshot.) Requests go through `witness_run.post` rather than LiteLLM: the body LiteLLM sends to an OpenAI-compatible server is the same three fields, and LiteLLM would add a disk cache and fetch a model price list from the internet at start-up.

## Prompt

System turn only, the runner's `--prompt` default, verbatim:

```
Transcribe the entirety of this historical document to XML format.
```

User turn: the image alone. (The paper harness class `ocr/systems/finetuned_ocr.py` sends a misspelled variant, "Transcribe the entiretly of this historical documents to XML format."; the runner does not.)

## Preprocessing

`load_image`: open the page, convert to RGB if it is not. `encode_image`: if either side exceeds 2,500 px, scale both by the smaller ratio with `int()` truncation (LANCZOS); encode as PNG (a converted image has no format; a JPEG source already in RGB stays JPEG at quality 95). Whole page, colour, no crop, no segmentation. vLLM then applies the snapshot's preprocessor bounds (min 401,408, max 4,014,080 pixels, patch 14 x merge 2).

## Decoding

- Sent: `temperature` 0.6 (`MODEL_MAP["churro"]["static_params"]`); no `max_tokens`, so vLLM answers to the end of the 20,000-token context.
- Not sent, so the server's generation config applies (vLLM's default `--generation-config auto` reads the snapshot's `generation_config.json`): `repetition_penalty` 1.05; top_p and top_k at vLLM defaults.
- Retries: LiteLLM's `num_retries=1` for a vLLM model, so one more try after a failed request. LiteLLM's timeout is 600 s (`utils/llm/config.py::DEFAULT_TIMEOUT`); the arm uses `--request-timeout`, default 1,800 s like the other bake-off arms, so a slow looping page under load is not cut off by the harness. `--request-timeout 600` gives the runner's limit.
- Concurrency 64; vLLM's default sequence limit.

## Output and normalisation

- Raw: `HistoricalDocument` XML (Metadata, Page → Header/Body/Footer → Paragraph/Line/…). Cached as `raw_response` (the server's body) and `answer`.
- The runner trims a duplicated prompt at the start of the answer (`trim_leading_prompt`).
- Text: `evaluation/xml_utils.py::extract_actual_text_from_xml`, carried in `churro_native.py` over lxml and checked here against the vendor's own function on five samples (identical output). Not XML (no `HistoricalDocument`): the answer as is. Otherwise: `<`, `>`, `&` escaped outside the schema's tags; `Description`, `Deletion`, `Illegible` and `Gap` removed with their content; parsed with lxml's recovering parser (so a truncated answer still yields its text); each page's Header, Body and Footer, in that order, each text node one stripped line; pages separated by a blank line. Then our `base.plain` collapses whitespace and drops empty lines.
- Consequence of the vendor rule: an inline element inside a `Line` (for example `Addition`) splits that line, and struck text is dropped.

## Resources

- VRAM: weights 7.5 GB; fits a 24 GB card (the repository's 24 GB serving row). At `--gpu-memory-utilization 0.9` the rest goes to KV cache.
- CPU: image preparation per page (PIL), light.
- Seconds per page: about 5-8 s/page throughput at concurrency 64 and about 2 min median latency, measured on an A40 with our `churro` arm. The native arm's 20,000-token context caps a looping page at roughly 15,000 to 19,000 output tokens instead of 25,000, so its tail should be shorter.

## Known failure modes

- Loops to the end of the context on some pages (seen at 25,000 tokens in our arm); here the loop ends at the 20,000-token context with `finish_reason` `length`.
- Temperature 0.6 makes answers vary between runs; there is no seed in the runner's request.
- A loop cut mid-element leaves malformed XML; the recovering parser keeps the text read so far.
- An empty answer or a second failure is cached as an empty page with the error.

## What differs from our vLLM adapter

| | our `churro` arm | `churro-native` |
|---|---|---|
| Prompt | same text (`registry-v0.3.0`) | same text (runner default) |
| Pixels | resize then RGB (v0.3.0 `prepare_ocr_image`), same 2,500 px rule | RGB then resize (runner), same rule; identical pixels for grey and colour pages |
| Sampling | temperature 1e-6 (raised to 0.01 by vLLM), repetition 1.05, top_k 50, top_p 1.0 (`config/decoding.toml`) | temperature 0.6, repetition 1.05 from the snapshot, top_k/top_p vLLM defaults |
| Answer bound | 25,000 tokens in a 32,768 context | none sent, 20,000 context |
| Retries | none | one, on a failed request |
| Timeout | 1,800 s | 1,800 s (`--request-timeout`; the runner's LiteLLM uses 600 s) |
| Remote code | not trusted | not trusted (the runner passes the flag; the snapshot has no code) |
| Server | sealed row: `--generation-config vllm`, pixel bounds sent, prefix caching on, 32 sequences, 0.92 memory | runner's flags: snapshot generation config, snapshot pixel bounds, vLLM defaults otherwise, 0.9 memory |
| Post-processing | `common/churro_document.py` reader | the repository's own `extract_actual_text_from_xml` |

## Untested here

No GPU here: the real model and the real vLLM server have not run. Tested on CPU: the arm in its own venv (real lxml) against `fake_vllm_server.py`, and the extractor port against the vendor's function. First command on the pod:

```
/workspace/venvs/churro_native/bin/python -m operations.bakeoff.native.churro_native run \
  --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store
```

## Our arm

- Module `operations/bakeoff/native/churro_native.py`, arm name `churro-native`, cache label `churro-native`.
- `check`, `install --venv-dir DIR`, `fetch --store-root DIR`, `prepare` (nothing to prepare), `run …`; extra flag `--max-model-len` (default the runner's 20,000).
- No telemetry on this path: the arm uses neither LiteLLM nor the vendor's Docker helpers; `HF_HUB_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1` are set for the arm and the server.

## Sources

- github.com/stanford-oval/Churro @ `c8b2f28834991ebc96faef31562662bddc8062ee`: `run_churro_ocr.py`, `pixi.toml`, `README.md`, `ocr/systems/finetuned_ocr.py`, `utils/llm/models.py`, `utils/llm/core.py`, `utils/llm/messages.py`, `utils/llm/config.py`, `utils/utils.py`, `utils/docker/vllm.py`, `utils/docker/servers.py`, `evaluation/xml_utils.py`, `evaluation/historical_doc.xsd` (SHA-256 `a62e5c41367fcc799b6591c9387c3170a531ee9da62e8243795e9f288dff61c9`); compared with `ed09bc7fd6475c333a25427f3d0b9227af46ce27` and tag `v0.3.0` = `4abb17386d9656199c2776195926545fc527a691` (`src/churro_ocr/templates/presets.py`, `src/churro_ocr/_internal/image.py`).
- huggingface.co/stanford-oval/churro-3B @ `ca2150ea465d5a3d67818c50e234b9422619c75d`: `README.md`, `config.json`, `generation_config.json`, `preprocessor_config.json`, `LICENSE`, file listing; commit history of `main`.
- This repository: `common/churro_document.py`, `common/imaging_ports.py`, `operations/bakeoff/arms.py`, `config/decoding.toml`, `config/serving_recipes_real.toml`.
