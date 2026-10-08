"""Party v2, page-wise recognition prompted with kraken blla baselines: arm `party-blla`.
Not a CTC model: a tiny Llama decoder generates each line byte by byte, so unlike the
CTC arms it can produce text that is not on the page.

    python -m operations.bakeoff.lines.party install
    python -m operations.bakeoff.lines.party fetch --store-root STORE
    python -m operations.bakeoff.lines.party run --pages DIR --out CACHE --store-root STORE \
        [--device cuda]

The blla segmentation (`blla.py`, kraken 7.1 environment) is shared with the other
`-blla` arms. Each page's ALTO is copied, every line is tagged French with Party's own
`set-lang`, and all pending pages go through one `party ocr` process (the model loads
once), the vendor's own command:

    party set-lang fra <copies>
    party [-d cuda:0 --precision bf16-mixed | -d cpu --threads N] ocr -l model.safetensors -a \
        -B 32 --prompt-mode curves --add-lang-token -i <in>.xml <out>.xml ...

Greedy decoding (argmax); Party's default of 512 generated tokens (UTF-8 bytes) per line,
which the base model's decoder clamps to its 384-token context.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import blla, harness
from operations.bakeoff.lines.kraken_ppocr import download_checked

ZENODO_RECORD = "20642057"
DOI = "10.5281/zenodo.20642057"
MODEL_FILE = "model.safetensors"
MODEL_SHA256 = "d6f3c2273687a79dd4852c4cfe63ec4c9e75a2a148fe02a8b787ab6afec236aa"
MODEL_MD5 = "cf165e67061d492b72f600a6a72b7c61"
PARTY_COMMIT = "c2589b1b515ed690f883c6afaef6c01ce29bf72d"
LANGUAGE = "fra"
BATCH_SIZE = 32
MAX_GENERATED_TOKENS = 512
# Party builds its network from these two Hub models before loading model.safetensors
# over them (`party/party.py` `PartyModel.__init__`, pretrained by default), fetching
# them by name with no revision. `fetch` puts both, at these commits, in a cache beside
# the weights so the offline run finds them.
BASE_MODELS = {
    "timm/swin_base_patch4_window7_224.ms_in22k_ft_in1k": "a6a1eb2321b4f556fa0fa243fb777d47679f13c9",
    "mittagessen/bytellama-43m-cc": "05e49f536fbb393a7127055f883d34606bba7712",
}
HUB_CACHE = "hf-cache"


class PartyRecogniser:
    def __init__(self, args: argparse.Namespace, weights: Path, runner: Any = None) -> None:
        import subprocess

        self.args = args
        self.model = weights / MODEL_FILE
        self.party = args.venv_dir / "bin" / "party"
        self.runner = runner or subprocess.run
        self.raw_dir = args.out / args.label / "_raw"
        self.log = args.out / args.label / "arm.log"

    def settings(self) -> dict[str, Any]:
        return {
            "model": MODEL_FILE,
            "model_sha256": MODEL_SHA256,
            "party": PARTY_COMMIT,
            "prompt_mode": "curves (blla baselines)",
            "language_token": LANGUAGE,
            "decoding": "greedy",
            "max_generated_tokens": MAX_GENERATED_TOKENS,
            "max_generated_tokens_effective": 384,
            "batch_size": BATCH_SIZE,
            "device": self.args.device,
            "precision": "bf16-mixed" if self.args.device == "cuda" else "32-true",
        }

    def ocr_argv(self, pairs: list[tuple[Path, Path]]) -> list[str]:
        argv = [str(self.party)]
        argv += blla.device_argv(self.args.device)
        if self.args.device == "cuda":
            argv += ["--precision", "bf16-mixed"]
        else:
            argv += ["--threads", str(self.args.threads)]
        argv += ["ocr", "-l", str(self.model), "-a", "-B", str(BATCH_SIZE)]
        argv += ["--prompt-mode", "curves", "--add-lang-token"]
        for source, dest in pairs:
            argv += ["-i", str(source), str(dest)]
        return argv

    def read(self, prepared: list[harness.Prepared]):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        pairs = []
        for item in prepared:
            source = self.args.out / "_lines" / "blla" / f"{item.page.stem}.xml"
            tagged = self.raw_dir / f"{item.page.stem}.in.xml"
            shutil.copyfile(source, tagged)
            answer = self.raw_dir / f"{item.page.stem}.xml"
            answer.unlink(missing_ok=True)
            pairs.append((tagged, answer))
        tag_argv = [str(self.party), "set-lang", LANGUAGE, *(str(t) for t, _ in pairs)]
        tagged_ok, tag_seconds = harness.subprocess_page(tag_argv, self.log, self.runner)
        argv = self.ocr_argv(pairs)
        done, seconds = (None, 0.0)
        if tagged_ok.returncode == 0:
            env = {"HF_HUB_CACHE": str(self.model.parent / HUB_CACHE)}
            done, seconds = harness.subprocess_page(argv, self.log, self.runner, env)
        lines = [max(1, len(item.lines["lines"])) for item in prepared]
        for item, (tagged, answer), count in zip(prepared, pairs, lines, strict=True):
            share = (seconds + tag_seconds) * count / sum(lines)
            request = {
                "line_source": "blla",
                "lines": len(item.lines["lines"]),
                "image": {"file": str(item.page), "sha256": harness.sha256_file(item.page)},
                "segmentation_sent": {"file": str(tagged), "sha256": harness.sha256_file(tagged)},
                "settings": self.settings(),
                "commands": [tag_argv, "party ocr (see server.argv)"],
            }
            if done is None or not answer.is_file():
                code = tagged_ok.returncode if done is None else done.returncode
                error = f"party exited {code}; no answer for this page (see arm.log)"
                request["unit"] = "page"
                yield (
                    item,
                    harness.PageResult([harness.unit(request, None, None, share, error)], argv),
                )
                continue
            yield item, harness.PageResult(blla.alto_units(item.page, answer, request, share), argv)


def fetch(dest: Path, _args: argparse.Namespace) -> Path:
    """The model from Zenodo (MD5 and SHA-256 checked) and Party's two base models into
    `<dest>/hf-cache`, each cache's `main` pointed at the pinned commit, because Party
    asks for them by name only."""
    from huggingface_hub import snapshot_download

    cache = dest / HUB_CACHE
    for repo, commit in BASE_MODELS.items():
        patterns = ["config.json", "model.safetensors"]
        snapshot_download(repo, revision=commit, cache_dir=cache, allow_patterns=patterns)
        refs = cache / f"models--{repo.replace('/', '--')}" / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(commit)
    target = dest / MODEL_FILE
    if target.is_file() and harness.sha256_file(target) == MODEL_SHA256:
        return target
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://zenodo.org/records/{ZENODO_RECORD}/files/{MODEL_FILE}?download=1"
    return download_checked(url, target, MODEL_MD5, MODEL_SHA256)


ARM = harness.Arm(
    module="operations.bakeoff.lines.party",
    repo=f"https://doi.org/{DOI}",
    revision=f"{MODEL_FILE}@sha256:{MODEL_SHA256}",
    artifact="party-v2",
    recipe=harness.VENVS / "party",
    python="3.12",
    pins={"party": "0.0.0.post492+gc2589b1", "kraken": "7.0.3", "torch": "2.12.0"},
    weight_files=(MODEL_FILE,),
    line_sources=("blla",),
    arm_name=lambda args: "party-blla",
    recogniser=PartyRecogniser,
    fetch=fetch,
)


def main(argv: list[str] | None = None, recogniser: Any = None) -> int:
    return harness.main(ARM, argv, recogniser)


if __name__ == "__main__":
    sys.exit(main())
