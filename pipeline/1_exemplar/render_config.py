"""Load the run-owned PDF render target without moving safety bounds into config."""

from __future__ import annotations

from pathlib import Path
from typing import Final, NamedTuple

from common.contracts.errors import ContractError
from common.sealed_config import read_sealed_toml

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
    """One run's resolved render target and the seal of the policy it came from.

    Settings and digest come from one read, so the sealed digest always names the
    bytes the settings were parsed from; a second open for the digest could see a
    rewrite and seal a configuration the run did not execute.
    """

    settings: PdfRenderSettings
    config_sha256: str


def load_pdf_render_binding(
    path: Path = DEFAULT_RENDER_CONFIG_PATH,
    *,
    target_override: int | None = None,
    minimum_dpi: int,
) -> PdfRenderBinding:
    """Read the policy once; parse and seal that one read.

    `config_sha256` is the seal of the file as read, not of the resolved
    settings: `--pdf-target-dpi` may override the configured target, and the run
    seals the override separately. What this digest answers is "which
    `pdf_render.toml` did this run parse", which is the question a point-of-use
    recheck asks.
    """
    try:
        document, digest = read_sealed_toml(path, "PDF render config")
    except ContractError as error:
        raise RenderConfigRefusal(str(error)) from error
    table = document.get("pdf")
    if set(document) != {"pdf"} or not isinstance(table, dict) or set(table) != {"target_dpi"}:
        raise RenderConfigRefusal(f"{path} must contain exactly one [pdf] table with target_dpi")
    configured = table["target_dpi"] if target_override is None else target_override
    if not isinstance(configured, int) or isinstance(configured, bool) or configured <= 0:
        source = "--pdf-target-dpi" if target_override is not None else f"{path} target_dpi"
        raise RenderConfigRefusal(f"{source} must be a positive whole DPI")
    return PdfRenderBinding(
        PdfRenderSettings(configured, max(configured, minimum_dpi), minimum_dpi),
        digest,
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
