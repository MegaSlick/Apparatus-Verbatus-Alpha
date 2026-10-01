"""The images one reader call carried, in the order it sent them."""

from __future__ import annotations

from typing import Any


def presented_image_refs(autopsia: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The page renders and the region crops one reader call carries, each in send order.

    A call sends every view's page renders, then its text, then every view's region
    crops, and names their digests in that same order; the reader, the resume check and
    the re-proof validator all take the order from here.
    """
    views = autopsia["views"]
    return (
        [ref for view in views for ref in view["page_render_refs"]],
        [ref for view in views for ref in view["region_refs"]],
    )


def presented_image_sha256s(autopsia: dict[str, Any]) -> list[str]:
    """The image digests one reader call about this act carries, in the order sent."""
    pages, regions = presented_image_refs(autopsia)
    return [ref["sha256"] for ref in pages + regions]
