# dots.mocr run card

**Verdict in one line:** ready-untested-here (the vendor's own parser path runs end to end against the fake server on CPU, PyMuPDF render included; no GPU run yet; the licence's use policy wants the lead's reading)

## Identity

- Repo `dots-studio/dots.mocr` (the README's `rednote-hilab/dots.mocr` redirects there), pinned revision `e539fbb52280393adc081b289ec597430a0f9031`. The weights and code were uploaded at `c59816f805ae4e7f9e0cc965b8a462cffc4ad414`; every later commit only adds `.eval_results/` (checked by comparing the two trees). `dots.mocr` is the renamed `dots.ocr-1.5` (vendor README); `dots-studio/dots.ocr` is the 2025 model.
- Code: github.com/rednote-hilab/dots.mocr at `23f3e5612fb8066d4034d5ecfc8f33a9243533eb` (the repository's head; four commits, all from the release week).
- Licence: MIT (model card metadata and the GitHub `LICENSE`) **plus** the "dots.mocr LICENSE AGREEMENT" shipped with the weights, which says the MIT licence prevails on conflict and adds an acceptable-use policy: no extraction of personal sensitive information "without legal authorization or consent, for purposes of privacy violation, automated discriminatory decision-making, or harassment" (3.3 b), and no "unauthorized digitization of publications/document scanning or bulk scraping" of copyright material (3.3 c). Commercial use is allowed. Whether a transcription bench over civil registers sits inside 3.3 is the lead's call.
- Size on disk: 6.1 GB (two bf16 safetensors). Family: VLM (Qwen2-1.5B-class language model with a 42-layer, 1.2 B-parameter vision encoder; `DotsOCRForCausalLM`). Its `config.json` is identical to `dots.ocr`'s.

## Install

- Own venv for the client: `operations/bakeoff/native/venvs/dots-mocr/pyproject.toml` with `uv.lock` (produced here, linux x86_64): `pymupdf==1.26.4`, `cairosvg==2.8.2`, `openai==2.2.0`, `requests==2.32.5`, `pydantic==2.12.3`, `numpy==2.3.4`, `tqdm==4.67.1`, `pillow==12.3.0`, 39 packages in the lock: what `import dots_mocr` needs on its vLLM path.
- The vendor package is not built: its `setup.py` (`find_packages()`) leaves out `dots_mocr/model`, which has no `__init__.py`, so a wheel cannot import it. The vendor's own instruction is `pip install -e .` in a clone; `install` does the equivalent: `uv sync --locked`, then a checkout of the pinned commit into `<venv>/src/dots-mocr` and a `.pth` file pointing at it.
- Vendor's own pins (`requirements.txt`): `gradio, PyMuPDF, openai, qwen_vl_utils, transformers==4.57.6, huggingface_hub, modelscope, accelerate, cairosvg` (flash-attn commented out). The transformers, gradio, modelscope and accelerate pins serve the demos and the transformers path, not this one.
- The vLLM server runs from the project pod venv (vLLM 0.30.0, transformers 5.14.1); the vendor names vLLM 0.11.0 or later.
- Install: `.venv/bin/python -m operations.bakeoff.native.dots_mocr install --venv-dir /workspace/venvs/dots_mocr` (about a minute plus a clone).
- Risks: `cairosvg` needs the system cairo library (`apt-get install -y libcairo2` on a bare image; `check` reports it); PyMuPDF is AGPL-3.0 (run on the pod only, never distributed or vendored).

## Native inference path

Read at `23f3e56`: `dots_mocr/parser.py` (`DotsMOCRParser.__init__` defaults, `_parse_single_image`, `parse_image`, `parse_file`, `main` with its command-line defaults), `dots_mocr/model/inference.py::inference_with_vllm`, `dots_mocr/utils/prompts.py`, `dots_mocr/utils/image_utils.py` (`fetch_image`, `smart_resize`, `PILimage_to_base64`, `get_image_by_fitz_doc`), `dots_mocr/utils/doc_utils.py` (`fitz_doc_to_image`, `get_matrix`), `dots_mocr/utils/layout_utils.py` (`post_process_output`, `post_process_cells`), `dots_mocr/utils/format_transformer.py::layoutjson2md`, `dots_mocr/utils/output_cleaner.py`, `dots_mocr/utils/consts.py`, `README.md`.

The vendor command is `python3 dots_mocr/parser.py IMAGE` (prompt `prompt_layout_all_en`, temperature 0.1, top_p 1.0, 16,384 tokens, 16 threads, fitz preprocessing at 200 dpi). Our arm calls the vendor's functions in that order, per page, and sends the body `inference_with_vllm` builds through `witness_run.post`. The server is the README's line:

```
vllm serve <snapshot> --host 127.0.0.1 --port 8195 --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.9 --chat-template-content-format string \
  --served-model-name model --trust-remote-code
```

**Remote code.** vLLM 0.30.0 has the model (`vllm/model_executor/models/dots_ocr.py`, registered as `DotsOCRForCausalLM`) and a copy of its config class (`vllm/transformers_utils/configs/dotsocr.py`), but `dots_ocr` is not in its `_CONFIG_REGISTRY` (`vllm/transformers_utils/config.py`), and transformers 5.14.1 has no `dots_ocr` model type: loading the config without remote code fails ("contains custom code which must be executed", reproduced here with transformers 5.14.1 on the snapshot's `config.json`). So `--trust-remote-code` is needed, as the vendor's own command has it. What it runs is `configuration_dots.py` (read at the pinned revision): two config classes (`DotsVisionConfig`, `DotsOCRConfig` over `Qwen2Config`) and `DotsVLProcessor` (a `Qwen2_5_VLProcessor` that sets the image token ids), registered with `AutoProcessor` and `CONFIG_MAPPING`. It imports only `typing` and `transformers`: no network, file or process access. `modeling_dots_ocr.py` and `modeling_dots_vision.py` (torch modules, `flash_attn` imported at the top) are not loaded by vLLM, which uses its own model class; neither has network, file or process calls either. The snapshot folder must not contain a `.` in its name (vendor README: the remote code is imported by folder name), so the store layout uses `hf/DotsMOCR`.

## Prompt

`dict_promptmode_to_prompt["prompt_layout_all_en"]`, verbatim, sent after the vendor's image placeholder `<|img|><|imgpad|><|endofimg|>` in the same text part (the vendor's note: without it vLLM adds a newline), image part first, no system turn:

```
Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
```

(The string ends with a newline. The OCR-only mode, `prompt_ocr`, is "Extract the text content from this image." and drops page headers and footers; not used.)

## Preprocessing

- `fetch_image(path)`: open, convert to RGB (RGBA flattened on white). The vendor's `parse_file` accepts only `.jpg/.jpeg/.png`; the arm calls `fetch_image` on the `.tif` directly, the only step it skips.
- `get_image_by_fitz_doc(image, target_dpi=200)` (the CLI default): the image is saved as PNG, which carries no resolution, so PyMuPDF takes it as 96 dpi; the page is rendered at 200/96 = 2.08x unless its point area exceeds 11,289,600, and re-rendered at 0.75x (72/96) if the 200-dpi render is over 4,500 px on either side. Measured here with the real code: 400 x 300 → 834 x 625; 2000 x 1500 → 4167 x 3125; 3200 x 2400 → 2400 x 1800; 5000 x 7000 → 2840 x 3976. A TIFF's own dpi is ignored. `--no-fitz-preprocess` (the vendor's `--no_fitz_preprocess`) sends the page at its own size.
- The rendered image goes as PNG; vLLM resizes it within the snapshot's 3,136 to 11,289,600 pixels on a 28 px grid (up to 14,400 image tokens).

## Decoding

`max_completion_tokens` 16,384, temperature 0.1, top_p 1.0 (the CLI's defaults; the demo script uses top_p 0.9 and 32,768). No `--max-model-len`, so vLLM takes the model's 131,072. No retries. 16 pages in flight (the CLI's `--num_thread`); vLLM's default sequence limit.

## Output and normalisation

- Raw: a JSON list of cells `{"bbox": [x1, y1, x2, y2], "category": …, "text": …}` in reading order (bbox in the sent image's pixels). Cached as `raw_response`, `answer`, and the vendor's `cells` (bboxes mapped back to the page by `post_process_cells`).
- The vendor reads it with `post_process_output`: `json.loads` and bbox mapping; if that fails (a loop cut at the token bound), its `OutputCleaner` salvages the cells it can and joins their texts with blank lines (`json_salvaged: true`).
- The vendor's text output is `layoutjson2md`: cell texts in order joined by blank lines, Picture as an embedded image, Formula wrapped in `$$`, headers and footers kept in `.md` and dropped in `_nohf.md`.
- Our rule (`dots_mocr.cells_lines`), on the vendor's cells, in their order: Picture skipped; Table HTML, each row a line with its cells joined by a space; Formula LaTeX kept as written without `$$`; every other category read as Markdown with heading marks, bullets, `**`/`__`/`~~` and backslash escapes removed and its own line breaks kept; page headers and footers kept (the `.md`, not the `_nohf.md`). A salvaged answer is read the same way (table rows split, then Markdown); if the vendor's cleaner itself fails it returns the raw answer, and then only the cells' `"text"` values are kept, so no JSON reaches `text`.

## Resources

- VRAM: weights 6.1 GB; fits a 24 GB card at `--gpu-memory-utilization 0.9` (KV cost about 28 KB per token, so one 131,072-token sequence is about 3.7 GB). No need for the 48 GB card.
- CPU: the PyMuPDF render per page, light to moderate on large scans.
- Seconds per page: no measurement exists. Estimate 5-10 s/page throughput at 16 in flight on the A40, by analogy with Churro-3B (similar size) with more image tokens (up to 14,400 against Churro's 5,120) and JSON overhead in the answer. The smoke run gives the first figure.

## Known failure modes

- Loops until 16,384 tokens leave broken JSON; the vendor's cleaner salvages part of it, and `loop` is set from `finish_reason` `length`.
- The prompt and training are document-parsing (printed pages, tables, formulas); handwriting quality is unknown.
- The fitz render can upscale small scans 2.08x and downscale large ones to 75%, whatever their real dpi.
- `--trust-remote-code` is required (see above).
- A cell list that is a JSON object instead of a list fails the vendor's assertion and goes to the salvage path.

## What differs from our vLLM adapter

The bake-off's own vLLM arms have no dots.mocr adapter (the bake-off README's "Not done yet"); nearest neighbour: `qwen-blind` with `--prompt-file`, which would send a different message shape (no placeholder, `openai` content format), no fitz render and greedy decoding. The pipeline now has one, `dots-mocr.v1` (`pipeline/3_attestatores/dots.py`, `common/dots_layout.py`), for seating dots.mocr on index and table pages by witness routing (`pipeline/3_attestatores/CONTRACT.md`): the same prompt, placeholder, sampling and text rule as this arm, but the page sent at its own size (the vendor's `--no_fitz_preprocess` path), so its readings are not the ones this arm scored.

## Untested here

No GPU here: the real model and server have not run, and `--trust-remote-code` loading on vLLM 0.30 has been reasoned from the source, not run. Tested on CPU: the arm in its own venv with the real `dots_mocr` code (PyMuPDF render included) against `fake_vllm_server.py`, and with a stand-in package in the project tests. First commands on the pod:

```
apt-get install -y libcairo2   # only if `check` reports cairo missing
/workspace/venvs/dots_mocr/bin/python -m operations.bakeoff.native.dots_mocr fetch --store-root $V/model-store
/workspace/venvs/dots_mocr/bin/python -m operations.bakeoff.native.dots_mocr run \
  --limit 2 --pages $V/bakeoff-pages --out $V/bakeoff/witness-cache --store-root $V/model-store
```

## Our arm

- Module `operations/bakeoff/native/dots_mocr.py`, arm name `dots-mocr`, cache label `dots-mocr`.
- `check`, `install --venv-dir DIR`, `fetch --store-root DIR` (into `hf/DotsMOCR`), `prepare` (nothing to prepare), `run …`; extra flag `--no-fitz-preprocess`.
- No telemetry: the vendor code reaches the network only for `http(s)` image paths (`fetch_image`, `get_image_by_fitz_doc`) and through its OpenAI client, neither used here. `HF_HUB_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1` are set for the arm and the server.

## Sources

- huggingface.co/dots-studio/dots.mocr @ `e539fbb52280393adc081b289ec597430a0f9031`: `README.md`, `config.json`, `generation_config.json`, `preprocessor_config.json`, `chat_template.json`, `configuration_dots.py`, `modeling_dots_ocr.py`, `modeling_dots_vision.py`, `dots.mocr LICENSE AGREEMENT`, `NOTICE`, file listing; trees at `c59816f805ae4e7f9e0cc965b8a462cffc4ad414` and `e539fbb…` compared; commit history of `main`; `dots-studio/dots.ocr` `config.json` and `preprocessor_config.json` at `main` for comparison.
- github.com/rednote-hilab/dots.mocr @ `23f3e5612fb8066d4034d5ecfc8f33a9243533eb`: `README.md`, `LICENSE`, `requirements.txt`, `setup.py`, `demo/demo_vllm.py`, `dots_mocr/__init__.py`, `dots_mocr/parser.py`, `dots_mocr/model/inference.py`, `dots_mocr/utils/*.py` as listed above. github.com/rednote-hilab/dots.ocr @ `36d7248878f181c6257dc1cbd799ae940970444f` (`README.md`, `demo/launch_model_vllm.sh`) for the rename.
- vllm-project/vllm @ `v0.30.0`: `vllm/model_executor/models/registry.py`, `vllm/model_executor/models/dots_ocr.py`, `vllm/transformers_utils/config.py`, `vllm/transformers_utils/configs/__init__.py`, `vllm/transformers_utils/configs/dotsocr.py`.
- huggingface/transformers @ `v5.14.1`: `src/transformers/models/auto/configuration_auto.py` (no `dots_ocr`), and the installed 5.14.1 wheel for the remote-code check.
