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


def page_units(arm: Arm, page_png: bytes, records: list[dict[str, Any]] | None = None):
    """The images this arm is shown for one page: one whole page, or each record crop.

    A record arm with an empty record list gets no unit (a valid, empty page); a
    whole-page fallback is a record whose bounds are the page (`whole_page_record`).
    """
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
    if arm.name == "qwen-blind":
        return [{"unit": "page", "bounds": whole, "png": convert_png_to_rgb(page_png)}]
    if arm.name == "dai":
        feeding = _feeding()
        units = []
        for index, record in enumerate(records or []):
            bounds = {k: record[k] for k in ("x", "y", "w", "h")}
            crop = crop_png(page_png, bounds)
            target = feeding.dai_dimensions(bounds["w"], bounds["h"])
            if target != (bounds["w"], bounds["h"]):
                crop = resize_png_lanczos(crop, *target)
            name = "whole-page" if record.get("fallback") else f"record-{index}"
            units.append({"unit": name, "bounds": bounds, "png": crop})
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
    return {"user": prompt_text or QWEN_BLIND_PROMPT}


def _sampling(arm: Arm) -> dict[str, Any]:
    if arm.name == "qwen-blind":
        return {"temperature": 0.0, "top_p": 1.0, "top_k": 0, "seed": 0}
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the chat-completions body, the request record cached beside the answer).

    `max_tokens` is the vendor's declared bound when it surely fits beside the prompt
    (prompt text counted as one token per byte, an over-count); otherwise it is left
    out and vLLM answers up to the context's remainder -- the pipeline's own rule.
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
    body: dict[str, Any] = {"model": served_name, "messages": messages, **_sampling(arm)}
    if arm.name in ("chandra", "qwen-blind"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if arm.name == "dai":
        body.update(_feeding().dai_wire_stop_token_ids())
    width, height = _size(png)
    declared = _declared_max_tokens(arm)
    text_bytes = sum(len(t.encode("utf-8")) for t in prompt.values())
    estimate = image_tokens(row, width, height) + text_bytes + 128
    if estimate + declared <= max_model_len:
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
    return body, record


# --- DAI's record detector (CPU) ----------------------------------------------------


class RecordDetector:
    """Teklia's YOLO OBB record detector, loaded the way the Designator loads it.

    `conf` and `imgsz` default to the serving row's values (Ultralytics' own defaults
    for this checkpoint); a bake-off arm may lower the confidence or raise the image
    size to see what the detector finds on pages unlike its training spreads.
    """

    def __init__(
        self,
        weights_dir: Path,
        threads: int = 1,
        *,
        conf: float | None = None,
        imgsz: int | None = None,
    ) -> None:
        from operations.serving import detector as d

        path = weights_dir / d.RECORD_DETECTOR_WEIGHTS_FILE
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != d.RECORD_DETECTOR_WEIGHTS_SHA256:
            raise SystemExit(f"record detector weights at {path} are not the pinned file")
        with open(ROOT / "config" / "serving_recipes_real.toml", "rb") as handle:
            rows = tomllib.load(handle)["profiles"]
        self.profile = next(r for r in rows if r.get("chair") == DETECTOR_CHAIR)
        self.settings = {
            "imgsz": imgsz or self.profile["imgsz"],
            "conf": conf if conf is not None else self.profile["conf_bp"] / 10_000,
            "iou": self.profile["iou_bp"] / 10_000,
            "max_det": self.profile["max_det"],
        }
        import torch

        torch.set_num_threads(threads)
        self.model = d.offline_ultralytics()(str(path), task=self.profile["task"])
        self._to_rgb = d.convert_page_to_rgb

    def records(self, page_png: bytes) -> list[dict[str, Any]]:
        """Each record's box in reading order, with the detector's confidence."""
        result = self.model.predict(
            self._to_rgb(page_png),
            device="cpu",
            verbose=False,
            **self.settings,
        )[0].obb
        width, height = _size(page_png)
        corners = result.xyxyxyxy.tolist()
        scores = result.conf.tolist() if hasattr(result, "conf") else [None] * len(corners)
        boxes = []
        for shape, score in zip(corners, scores, strict=True):
            bounds = obb_bounds(shape, width, height)
            if bounds:
                boxes.append({**bounds, "score": None if score is None else round(score, 4)})
        return order_records(boxes, width)


def whole_page_record(width: int, height: int) -> dict[str, Any]:
    """The page itself as one record, for an act page where the detector found none."""
    return {"x": 0, "y": 0, "w": width, "h": height, "score": None, "fallback": "whole-page"}


def obb_bounds(corners: list, width: int, height: int) -> dict[str, int] | None:
    """Floor and clamp each corner, then the half-open enclosing box (the Designator's rule)."""
    xs = [min(width - 1, max(0, math.floor(x))) for x, _ in corners]
    ys = [min(height - 1, max(0, math.floor(y))) for _, y in corners]
    if len(set(zip(xs, ys, strict=True))) < 3:
        return None
    x0, y0 = min(xs), min(ys)
    w, h = min(width, max(xs) + 1) - x0, min(height, max(ys) + 1) - y0
    return {"x": x0, "y": y0, "w": w, "h": h} if w > 0 and h > 0 else None


def order_records(boxes: list[dict[str, Any]], page_width: int) -> list[dict[str, Any]]:
    """Page reading order: left column before right on a spread, then top to bottom.

    A box is in the right column when it starts right of the middle and some other box
    ends left of it; otherwise every box is one column.
    """
    middle = page_width / 2

    def column(box: dict[str, int]) -> int:
        left_exists = any(o["x"] + o["w"] <= box["x"] for o in boxes if o is not box)
        return 1 if box["x"] >= middle * 0.9 and left_exists else 0

    return sorted(boxes, key=lambda b: (column(b), b["y"], b["x"]))
