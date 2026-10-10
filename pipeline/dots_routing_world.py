"""A synthetic roster that seats dots.mocr on index and table pages only.

Test support, not pipeline code: it writes, under a test's own directory, the
committed fixture roster with a fourth witness chair (`attestator_4`, adapter
`dots-mocr.v1`), routed by `[witness_routing]` or not, and a serving catalogue
with that chair's fixture rows. The synthetic fixture's `dots-*` scenarios
(`proof/build_fixture.py`) make page 2 a table page, a page the record
detector finds nothing on, or a table page whose dots.mocr answer was cut off.
Nothing committed seats the chair, so a run without these files is the run it
always was.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOTS_CHAIR = "attestator_4"
TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")


def models_config(directory: Path, *, routed: bool = True) -> Path:
    """The committed fixture roster with `attestator_4` (dots.mocr) seated, routed or not."""
    from common.chairs.manifests import build_manifest, write_manifest

    shutil.copytree(ROOT / "config" / "model-fixtures", directory / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", directory / "manifests")
    snapshot = directory / "model-fixtures" / DOTS_CHAIR
    snapshot.mkdir()
    (snapshot / "identity.txt").write_bytes(f"fixture chair: {DOTS_CHAIR}\n".encode())
    pin = write_manifest(build_manifest(snapshot), directory / "manifests" / f"{DOTS_CHAIR}.json")
    text = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    chair = f"""
[chairs.{DOTS_CHAIR}]
state = "configured"
source = "local-repository"
path = "{DOTS_CHAIR}"
digest_manifest = "{pin}"
manifest = "manifests/{DOTS_CHAIR}.json"
serving_recipe = "fake-attestatores-v0"
license_note = "fixture identity only; no model weights or model license apply"
witness_adapter = "dots-mocr.v1"
witness_scope = "page"
"""
    routing = f'\n[witness_routing]\n{DOTS_CHAIR} = "index-and-table.v1"\n' if routed else ""
    # Top-level tables go before the commented real roster at the end; TOML
    # takes them anywhere after the top-level keys.
    path = directory / "models.toml"
    path.write_text(text + chair + routing, encoding="utf-8")
    return path


def serving_recipes(directory: Path) -> Path:
    """The committed fixture catalogue with `attestator_4`'s fixture row at every tier."""
    rows = "".join(
        f"""
[[profiles]]
kind = "fixture"
recipe = "fake-attestatores-v0"
chair = "{DOTS_CHAIR}"
tier = "{tier}"
description = "offline walking-skeleton fixture for Attestator 4 (dots.mocr)"
"""
        for tier in TIERS
    )
    path = directory / "serving_recipes.toml"
    path.write_text(
        (ROOT / "config" / "serving_recipes.toml").read_text(encoding="utf-8") + rows,
        encoding="utf-8",
    )
    return path
