"""Load the run-owned PDF render target without moving safety bounds into config."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final, NamedTuple

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

DEFAULT_RENDER_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "pdf_render.toml"
)


class RenderConfigRefusal(ContractError):
    """The configured PDF target is absent, malformed, or not a whole DPI."""


class PdfRenderSettings(NamedTuple):
    """The requested run setting and the code-bounded target the renderer uses."""

    configured_target_dpi: int
    target_dpi: int
    minimum_dpi: int

    def to_record(self) -> dict[str, int]:
        return {
            "configured_target_dpi": self.configured_target_dpi,
            "target_dpi": self.target_dpi,
            "minimum_dpi": self.minimum_dpi,
        }


class PdfRenderBinding(NamedTuple):
    """One run's resolved render target and the digest of the bytes it came from.

    The two travel together because they must be of the *same read*. The door used
    to parse the settings here and then let `run_config_bindings` open the file
    again for its digest; a rewrite between those two reads produced a run whose
    `render_settings` recorded one target and whose `config_digest` bound the bytes
    of another, so a proof run claimed a configuration it did not execute (audit
    S6, reproduced with a one-DPI rewrite landing between the reads while the door
    still exited 0).
    """

    settings: PdfRenderSettings
    config_sha256: str


def load_pdf_render_binding(
    path: Path = DEFAULT_RENDER_CONFIG_PATH,
    *,
    target_override: int | None = None,
    minimum_dpi: int,
) -> PdfRenderBinding:
    """Read the policy once; parse and hash those same bytes.

    `config_sha256` is the digest of the file as read, not of the resolved
    settings: `--pdf-target-dpi` may override the configured target, and the run
    seals the override separately. What this digest answers is "which
    `pdf_render.toml` did this run parse", which is the question a point-of-use
    recheck asks.
    """
    try:
        raw = Path(path).read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RenderConfigRefusal(
            f"the PDF render config at {path} could not be read: {error}"
        ) from error
    table = document.get("pdf")
    if set(document) != {"pdf"} or not isinstance(table, dict) or set(table) != {"target_dpi"}:
        raise RenderConfigRefusal(f"{path} must contain exactly one [pdf] table with target_dpi")
    configured = table["target_dpi"] if target_override is None else target_override
    if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
        source = "--pdf-target-dpi" if target_override is not None else f"{path} target_dpi"
        raise RenderConfigRefusal(f"{source} must be a positive whole DPI")
    return PdfRenderBinding(
        PdfRenderSettings(configured, max(configured, minimum_dpi), minimum_dpi),
        digest_bytes(raw),
    )


def load_pdf_render_settings(
    path: Path = DEFAULT_RENDER_CONFIG_PATH,
    *,
    target_override: int | None = None,
    minimum_dpi: int,
) -> PdfRenderSettings:
    """Resolve one run's target, clamping only against the code-owned floor.

    Callers that seal a run want `load_pdf_render_binding` instead: the digest of
    the bytes these settings were parsed from is what makes the seal provable.
    """
    return load_pdf_render_binding(
        path, target_override=target_override, minimum_dpi=minimum_dpi
    ).settings
