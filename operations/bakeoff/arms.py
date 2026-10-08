"""What each bake-off arm sends: its vendor's own prompt, image preparation and sampling.

Every arm runs natively, outside the pipeline. The prompt bytes, image rules and
sampling values are the ones the repository already carries for each witness, imported
rather than retyped wherever they are importable without a run context:

- chandra: `common.chandra_layout.OCR_LAYOUT_PROMPT` (single user turn, image first),
  `common.chandra_presentation.render_page` (the vendor's `scale_to_fit`), thinking off
  (`common.chair_wire`), the first attempt's sampling only (no retry loop).
- churro: the `registry-v0.3.0` system string (`common.churro_document`), image-only user
  turn, `common.imaging_ports.resize_to_fit_churro` then RGB.
- dai: `feeding.dai_prompt()` (Teklia's system.txt and query.txt), the 1,500 px width rule
  (`feeding.dai_dimensions`), the second EOS id; one request per record crop from Teklia's
  own YOLO record detector run on the CPU (`operations.serving.detector`).
- qwen-blind: any vLLM vision model with a plain verbatim prompt, greedy, thinking off,
  `max_tokens` 12,288: the reader with no witnesses.
- qwen-vendor: the same reader as its vendor documents a page transcription: the Qwen
  cookbook's plain-text OCR instruction around the project's verbatim rules, no system
  prompt, the model card's non-thinking sampling preset, the checkpoint's own pixel
  bounds, thinking off, and no reply cap (the context's remainder). `--repo` picks the
  family preset (`VENDOR_PRESETS`).

Sampling comes from `config/decoding.toml` (the sealed per-chair rows); answer bounds from
`common.request_capacity.DECLARED_ANSWER_BOUND_TOKENS`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_ATTESTATORES = ROOT / "pipeline" / "3_attestatores"

QWEN_BLIND_PROMPT = (
    "Transcribe all the handwritten and printed text on this page exactly as written.\n"
    "- Keep the original spelling, accents, abbreviations, punctuation and capitalisation; "
    "do not modernise, expand or correct anything.\n"
    "- Write one output line for each written line on the page, in reading order.\n"
    "- Write [[?]] where the ink cannot be read.\n"
    "- Output only the transcription: no commentary, no markup, no reasoning."
)
QWEN_BLIND_MAX_TOKENS = 12_288

# The Qwen3-VL OCR cookbook's plain-text instruction, then the project's verbatim rules.
QWEN_VENDOR_PROMPT = (
    "Please output only the text content from the image without any additional "
    "descriptions or formatting.\n"
    "- Keep the original spelling, accents, abbreviations, punctuation and capitalisation "
    "exactly as written; do not modernise, expand or correct anything.\n"
    "- Write one output line for each written line on the page, in reading order.\n"
    "- Write [[?]] where the ink cannot be read."
)
_QWEN_COOKBOOK = (
    "https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/"
    "cookbooks/ocr.ipynb"
)
# Each model card's "Instruct (or non-thinking) mode" row, and the pixel bounds of the
# checkpoint's own preprocessor_config.json (size.shortest_edge / size.longest_edge).
_NON_THINKING = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}
VENDOR_PRESETS: dict[str, dict[str, Any]] = {
    "Qwen/Qwen3.8-": {
        "family": "qwen3.8",
        "sampling": _NON_THINKING,
        "min_pixels": 65_536,
        "max_pixels": 16_777_216,
        "prompt_source": _QWEN_COOKBOOK,
        "card": "https://huggingface.co/Qwen/Qwen3.8-27B/blob/"
        "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0/README.md",
    },
    "Qwen/Qwen3.5-": {
        "family": "qwen3.5",
        "sampling": _NON_THINKING,
        "min_pixels": 65_536,
        "max_pixels": 16_777_216,
        "prompt_source": _QWEN_COOKBOOK,
        "card": "https://huggingface.co/Qwen/Qwen3.5-27B/blob/"
        "fc05daec18b0a78c049392ed2e771dde82bdf654/README.md",
    },
}


def vendor_preset(repo: str | None) -> dict[str, Any]:
    """The family preset for a repo id; a repo outside every family is refused."""
    for prefix, preset in VENDOR_PRESETS.items():
        if repo and repo.startswith(prefix):
            return preset
    raise SystemExit(f"qwen-vendor has no preset for {repo!r}; known: {sorted(VENDOR_PRESETS)}")


@dataclass(frozen=True)
class Arm:
    name: str
    chair: str  # the serving row (config/serving_recipes_real.toml) it borrows
    artifact: str | None  # model-store artifact name (common/chairs/model_store.py)
    scope: str  # "page" or "record"
    default_tier: str


ARMS: dict[str, Arm] = {
    "chandra": Arm("chandra", "attestator_1", "chandra-ocr-2", "page", "generic-48gb"),
    "dai": Arm("dai", "attestator_2", "dai-recordgold-atr", "record", "generic-48gb"),
    "churro": Arm("churro", "attestator_3", "churro-3B", "page", "generic-48gb"),
    "qwen-blind": Arm("qwen-blind", "perlector", None, "page", "generic-80gb-plus"),
    "qwen-vendor": Arm("qwen-vendor", "perlector", None, "page", "generic-80gb-plus"),
}
DETECTOR_ARTIFACT = "yolov26-record-detection"
DETECTOR_CHAIR = "secondary_proposer"


def _feeding() -> Any:
    if str(_ATTESTATORES) not in sys.path:
        sys.path.insert(0, str(_ATTESTATORES))
    import feeding

    return feeding


def chair_identity(chair: str) -> dict[str, Any]:
    """The chair's repo, revision and manifest digest from config/models-real.toml."""
    with open(ROOT / "config" / "models-real.toml", "rb") as handle:
        chairs = tomllib.load(handle)["chairs"]
    row = chairs.get(chair, {})
    return {k: row.get(k) for k in ("repo", "revision", "digest_manifest")}


def serving_row(chair: str, tier: str) -> dict[str, Any]:
    with open(ROOT / "config" / "serving_recipes_real.toml", "rb") as handle:
        profiles = tomllib.load(handle)["profiles"]
    for row in profiles:
        if row.get("chair") == chair and row.get("tier") == tier and row.get("kind") == "vllm":
            return row
    raise SystemExit(f"no vLLM serving row for chair {chair!r} at tier {tier!r}")


def arm_row(arm: Arm, row: dict[str, Any], repo: str | None) -> dict[str, Any]:
    """The serving row as this arm serves it: qwen-vendor takes its family's pixel bounds."""
    if arm.name != "qwen-vendor":
        return row
    preset = vendor_preset(repo)
    return {**row, "min_pixels": preset["min_pixels"], "max_pixels": preset["max_pixels"]}


def resolve_weights(
    arm: Arm, explicit: Path | None, store_root: Path | None, cache_root: Path | None
) -> Path:
    """The local snapshot to serve: --weights, else the pod's chair cache, else the store.

    The chair cache is `<cache_root>/by-digest/<digest_manifest>` on container disk and
    the model store is `<store_root>/hf/<artifact>` on the volume, the layouts
    `common/chairs/registry.py` and `common/chairs/model_store.py` write.
    """
    if explicit is not None:
        return explicit
    candidates = []
    digest = chair_identity(arm.chair).get("digest_manifest") if arm.artifact else None
    if cache_root is not None and digest:
        candidates.append(cache_root / "by-digest" / digest)
    if store_root is not None and arm.artifact:
        candidates.append(store_root / "hf" / arm.artifact)
    for path in candidates:
        # A transformers snapshot has config.json; the YOLO record detector is one model.pt.
        if (path / "config.json").is_file() or (path / "model.pt").is_file():
            return path
    raise SystemExit(
        f"no weights for {arm.name}: pass --weights, or a --store-root/--cache-root holding "
        f"one of {[str(c) for c in candidates] or 'nothing (no artifact for this arm)'}"
    )


def server_argv(
    row: dict[str, Any],
    weights: Path,
    *,
    port: int,
    served_name: str,
    gpu_memory_utilization: float,
    max_num_seqs: int,
    max_model_len: int | None = None,
    max_num_batched_tokens: int | None = None,
) -> list[str]:
    """`vllm serve` arguments: the sealed row's shape, with the bake-off's busy-card knobs.

    Same flags as `operations/serving/manager.py::render_vllm_argv`, except that
    `--gpu-memory-utilization` and `--max-num-seqs` are the bake-off's (keep the card full)
    and the revision flags are dropped (the snapshot directory is the pin).
    """
    pixels = {"min_pixels": row["min_pixels"], "max_pixels": row["max_pixels"]}
    return [
        "serve", str(weights), "--tokenizer", str(weights),
        "--host", "127.0.0.1", "--port", str(port),
        "--served-model-name", served_name,
        "--dtype", row["dtype"], "--seed", str(row["seed"]),
        "--max-model-len", str(max_model_len or row["max_model_len"]),
        "--max-num-seqs", str(max_num_seqs),
        "--max-num-batched-tokens", str(max_num_batched_tokens or row["max_num_batched_tokens"]),
        "--gpu-memory-utilization", f"{gpu_memory_utilization:.2f}",
        "--mm-processor-kwargs", json.dumps(pixels, sort_keys=True, separators=(",", ":")),
        "--generation-config", row["generation_config"],
        "--no-enable-log-requests", "--enable-prompt-tokens-details",
        "--chat-template-content-format", "openai",
        "--enable-prefix-caching" if row["enable_prefix_caching"] else "--no-enable-prefix-caching",
        "--enforce-eager" if row["enforce_eager"] else "--no-enforce-eager",
        "--trust-remote-code" if row["trust_remote_code"] else "--no-trust-remote-code",
    ]  # fmt: skip


# --- images -----------------------------------------------------------------------


def load_page_png(path: Path) -> bytes:
    """Any Pillow-readable page (the set is LZW/G4 TIFF) as lossless PNG bytes."""
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        if image.mode == "1":
            image = image.convert("L")
        elif image.mode not in ("L", "LA", "P", "RGB", "RGBA"):
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PNG")
    return out.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    from common.imaging import dimensions

    return dimensions(png)


def page_units(arm: Arm, page_png: bytes, records: list[dict[str, int]] | None = None):
    """The images this arm is shown for one page: one whole page, or each record crop."""
    from common.imaging import convert_png_to_rgb, crop_png, resize_png_lanczos

    width, height = _size(page_png)
    whole = {"x": 0, "y": 0, "w": width, "h": height}
    if arm.name == "chandra":
        from common.chandra_presentation import render_page

        image, _ = render_page(page_png, whole)
        return [{"unit": "page", "bounds": whole, "png": image}]
    if arm.name == "churro":
        from common.imaging_ports import resize_to_fit_churro

        target = resize_to_fit_churro(width, height)
        image = convert_png_to_rgb(resize_png_lanczos(crop_png(page_png, whole), *target))
        return [{"unit": "page", "bounds": whole, "png": image}]
    if arm.name in ("qwen-blind", "qwen-vendor"):
        return [{"unit": "page", "bounds": whole, "png": convert_png_to_rgb(page_png)}]
    if arm.name == "dai":
        feeding = _feeding()
        units = []
        for index, bounds in enumerate(records or []):
            crop = crop_png(page_png, bounds)
            target = feeding.dai_dimensions(bounds["w"], bounds["h"])
            if target != (bounds["w"], bounds["h"]):
                crop = resize_png_lanczos(crop, *target)
            units.append({"unit": f"record-{index}", "bounds": bounds, "png": crop})
        return units
    raise SystemExit(f"unknown arm {arm.name!r}")


# --- requests ---------------------------------------------------------------------


def _image_part(png: bytes) -> dict[str, Any]:
    url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": url}}


def _prompt(arm: Arm, prompt_text: str | None) -> dict[str, str]:
    if arm.name == "chandra":
        from common import chandra_layout

        return {"user": chandra_layout.OCR_LAYOUT_PROMPT}
    if arm.name == "churro":
        from common import churro_document

        return {"system": churro_document.churro_system_prompt("registry-v0.3.0")}
    if arm.name == "dai":
        return dict(_feeding().dai_prompt())
    if arm.name == "qwen-vendor":
        return {"user": prompt_text or QWEN_VENDOR_PROMPT}
    return {"user": prompt_text or QWEN_BLIND_PROMPT}


def _sampling(arm: Arm, repo: str | None = None) -> dict[str, Any]:
    if arm.name == "qwen-blind":
        return {"temperature": 0.0, "top_p": 1.0, "top_k": 0, "seed": 0}
    if arm.name == "qwen-vendor":
        return {**vendor_preset(repo)["sampling"], "seed": 0}
    from common.decoding import chair_decoding, load_decoding_policy

    policy, _ = load_decoding_policy(ROOT / "config" / "decoding.toml")
    return {**chair_decoding(policy, arm.chair), "seed": 0}


def _declared_max_tokens(arm: Arm) -> int:
    if arm.name == "qwen-blind":
        return QWEN_BLIND_MAX_TOKENS
    from common.request_capacity import DECLARED_ANSWER_BOUND_TOKENS

    return DECLARED_ANSWER_BOUND_TOKENS[arm.chair]


def image_tokens(row: dict[str, Any], width: int, height: int) -> int:
    from common.request_capacity import smart_resize

    factor = row["patch_size"] * row["merge_size"]
    h, w = smart_resize(
        height, width, factor=factor, min_pixels=row["min_pixels"], max_pixels=row["max_pixels"]
    )
    return math.ceil(h * w / (factor * factor))


def build_request(
    arm: Arm,
    unit: dict[str, Any],
    *,
    row: dict[str, Any],
    served_name: str,
    max_model_len: int,
    prompt_text: str | None = None,
    repo: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the chat-completions body, the request record cached beside the answer).

    `max_tokens` is the vendor's declared bound when it surely fits beside the prompt
    (prompt text counted as one token per byte, an over-count); otherwise it is left
    out and vLLM answers up to the context's remainder -- the pipeline's own rule.
    qwen-vendor always leaves it out: no reply cap. `repo` picks qwen-vendor's preset.
    """
    prompt = _prompt(arm, prompt_text)
    png = unit["png"]
    messages: list[dict[str, Any]] = []
    if "system" in prompt:
        messages.append({"role": "system", "content": [{"type": "text", "text": prompt["system"]}]})
    user = [_image_part(png)]
    if "user" in prompt:
        user.append({"type": "text", "text": prompt["user"]})
    messages.append({"role": "user", "content": user})
    body: dict[str, Any] = {"model": served_name, "messages": messages, **_sampling(arm, repo)}
    if arm.name in ("chandra", "qwen-blind", "qwen-vendor"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if arm.name == "dai":
        body.update(_feeding().dai_wire_stop_token_ids())
    width, height = _size(png)
    text_bytes = sum(len(t.encode("utf-8")) for t in prompt.values())
    estimate = image_tokens(row, width, height) + text_bytes + 128
    declared = None if arm.name == "qwen-vendor" else _declared_max_tokens(arm)
    if declared is None:
        basis = "omitted: no reply cap, the context's remainder"
    elif estimate + declared <= max_model_len:
        body["max_tokens"] = declared
        basis = "declared-bound"
    else:
        basis = f"omitted: declared {declared} + prompt estimate {estimate} > {max_model_len}"
    record = {
        "unit": unit["unit"],
        "bounds": unit["bounds"],
        "prompt_sha256": hashlib.sha256(
            json.dumps(prompt, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "prompt_roles": sorted(prompt),
        "sampling": {k: v for k, v in body.items() if k not in ("model", "messages")},
        "max_tokens": body.get("max_tokens"),
        "max_tokens_basis": basis,
        "image_size": [width, height],
        "image_sha256": hashlib.sha256(png).hexdigest(),
        "image_tokens_estimate": image_tokens(row, width, height),
    }
    if arm.name == "qwen-vendor":
        preset = vendor_preset(repo)
        record["vendor_preset"] = {
            "repo": repo,
            "family": preset["family"],
            "prompt": prompt["user"],
            "prompt_source": preset["prompt_source"] if prompt_text is None else "--prompt-file",
            "sampling_source": preset["card"],
            "min_pixels": row["min_pixels"],
            "max_pixels": row["max_pixels"],
        }
    return body, record


# --- DAI's record detector (CPU) ----------------------------------------------------


class RecordDetector:
    """Teklia's YOLO OBB record detector, loaded the way the Designator loads it."""

    def __init__(self, weights_dir: Path, threads: int = 1) -> None:
        from operations.serving import detector as d

        path = weights_dir / d.RECORD_DETECTOR_WEIGHTS_FILE
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != d.RECORD_DETECTOR_WEIGHTS_SHA256:
            raise SystemExit(f"record detector weights at {path} are not the pinned file")
        with open(ROOT / "config" / "serving_recipes_real.toml", "rb") as handle:
            rows = tomllib.load(handle)["profiles"]
        self.profile = next(r for r in rows if r.get("chair") == DETECTOR_CHAIR)
        import torch

        torch.set_num_threads(threads)
        self.model = d.offline_ultralytics()(str(path), task=self.profile["task"])
        self._to_rgb = d.convert_page_to_rgb

    def records(self, page_png: bytes) -> list[dict[str, int]]:
        p = self.profile
        result = self.model.predict(
            self._to_rgb(page_png),
            imgsz=p["imgsz"],
            conf=p["conf_bp"] / 10_000,
            iou=p["iou_bp"] / 10_000,
            max_det=p["max_det"],
            device="cpu",
            verbose=False,
        )[0].obb
        width, height = _size(page_png)
        return order_records(
            [b for b in (obb_bounds(c, width, height) for c in result.xyxyxyxy.tolist()) if b],
            width,
        )


def obb_bounds(corners: list, width: int, height: int) -> dict[str, int] | None:
    """Floor and clamp each corner, then the half-open enclosing box (the Designator's rule)."""
    xs = [min(width - 1, max(0, math.floor(x))) for x, _ in corners]
    ys = [min(height - 1, max(0, math.floor(y))) for _, y in corners]
    if len(set(zip(xs, ys, strict=True))) < 3:
        return None
    x0, y0 = min(xs), min(ys)
    w, h = min(width, max(xs) + 1) - x0, min(height, max(ys) + 1) - y0
    return {"x": x0, "y": y0, "w": w, "h": h} if w > 0 and h > 0 else None


def order_records(boxes: list[dict[str, int]], page_width: int) -> list[dict[str, int]]:
    """Page reading order: left column before right on a spread, then top to bottom.

    A box is in the right column when it starts right of the middle and some other box
    ends left of it; otherwise every box is one column.
    """
    middle = page_width / 2

    def column(box: dict[str, int]) -> int:
        left_exists = any(o["x"] + o["w"] <= box["x"] for o in boxes if o is not box)
        return 1 if box["x"] >= middle * 0.9 and left_exists else 0

    return sorted(boxes, key=lambda b: (column(b), b["y"], b["x"]))
