"""Load the run-owned PDF render target without moving safety bounds into config."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final, NamedTuple

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


def load_pdf_render_settings(
    path: Path = DEFAULT_RENDER_CONFIG_PATH,
    *,
    target_override: int | None = None,
    minimum_dpi: int,
) -> PdfRenderSettings:
    """Resolve one run's target, clamping only against the code-owned floor."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
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
    return PdfRenderSettings(configured, max(configured, minimum_dpi), minimum_dpi)
