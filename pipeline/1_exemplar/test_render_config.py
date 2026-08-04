"""The PDF target is run configuration; safety bounds remain renderer code."""

import importlib.util
import sys
from pathlib import Path

import door
import pdf_render
import pytest
import render_config
from synthetic_sources import content_page_pdf

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _exemplar_module():
    spec = importlib.util.spec_from_file_location(
        "exemplar_render_config_test", Path(__file__).resolve().parent / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_default_is_documented_run_configuration():
    settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    assert settings.to_record() == {
        "configured_target_dpi": 400,
        "target_dpi": 400,
        "minimum_dpi": 72,
    }


def test_a_per_run_override_wins_without_weakening_the_code_owned_floor(tmp_path):
    configured = tmp_path / "render.toml"
    configured.write_text("[pdf]\ntarget_dpi = 300\n", encoding="utf-8")

    ordinary = render_config.load_pdf_render_settings(
        configured, target_override=275, minimum_dpi=72
    )
    below_floor = render_config.load_pdf_render_settings(
        configured, target_override=36, minimum_dpi=72
    )

    assert ordinary.to_record()["configured_target_dpi"] == ordinary.target_dpi == 275
    assert below_floor.configured_target_dpi == 36
    assert below_floor.target_dpi == below_floor.minimum_dpi == 72


@pytest.mark.parametrize("target", [0, -1, True, 1.5, "400"])
def test_a_non_positive_or_non_integer_target_is_not_a_run_setting(tmp_path, target):
    configured = tmp_path / "render.toml"
    configured.write_text("[pdf]\ntarget_dpi = 400\n", encoding="utf-8")
    with pytest.raises(render_config.RenderConfigRefusal, match="positive whole DPI"):
        render_config.load_pdf_render_settings(configured, target_override=target, minimum_dpi=72)


def test_the_page_record_traces_requested_bounded_and_effective_dpi():
    settings = render_config.PdfRenderSettings(36, 72, 72)
    opened = pdf_render.open_document(content_page_pdf(b"", width=72, height=36))
    try:
        rendered = pdf_render.render_page(opened, 0, settings)
    finally:
        pdf_render.close_document(opened)

    assert rendered.contract["configured_target_dpi"] == 36
    assert rendered.contract["dpi"] == 72
    assert rendered.contract["effective_dpi"] == 72


def test_a_large_configured_target_is_still_capped_by_page_memory_bounds():
    settings = render_config.PdfRenderSettings(10_000, 10_000, 72)
    opened = pdf_render.open_document(content_page_pdf(b"", width=2384, height=3370))
    try:
        rendered = pdf_render.render_page(opened, 0, settings)
    finally:
        pdf_render.close_document(opened)

    assert 72 <= rendered.contract["effective_dpi"] < settings.target_dpi
    assert rendered.width * rendered.height <= pdf_render.MAX_PIXELS


def test_exemplar_refuses_a_page_target_that_disagrees_with_run_authority():
    settings = render_config.PdfRenderSettings(36, 72, 72)
    opened = pdf_render.open_document(content_page_pdf(b"", width=72, height=36))
    try:
        rendered = pdf_render.render_page(opened, 0, settings)
    finally:
        pdf_render.close_document(opened)
    run = {
        "render_settings": {
            "pdf": {
                "configured_target_dpi": 50,
                "target_dpi": 72,
                "minimum_dpi": 72,
            }
        }
    }

    with pytest.raises(ContractError, match="sealed pixel recipe"):
        _exemplar_module()._verify_render_contract(
            rendered.contract,
            0,
            {"geometry": {"width": rendered.width, "height": rendered.height}},
            run,
            container_format="pdf",
        )


def test_run_override_is_sealed_in_authority_and_changed_resume_writes_nothing(
    tmp_path, monkeypatch
):
    run_root = tmp_path / "runs"
    base = [
        "door.py",
        "--run-root",
        str(run_root),
        "--run-id",
        "configured-render",
        "--fixture-root",
        str(ROOT / "proof"),
        "--pdf-target-dpi",
        "275",
    ]
    monkeypatch.setattr(sys, "argv", base)
    assert door.main() == 0
    run = RunTree(run_root, "configured-render").read_run()
    assert run["render_settings"] == {
        "pdf": {
            "configured_target_dpi": 275,
            "target_dpi": 275,
            "minimum_dpi": 72,
        }
    }
    before = {
        path.relative_to(run_root): digest_bytes(path.read_bytes())
        for path in run_root.rglob("*")
        if path.is_file()
    }

    monkeypatch.setattr(sys, "argv", [*base[:-1], "300"])
    with pytest.raises(ContractError, match="config_digest|render_settings"):
        door.main()
    after = {
        path.relative_to(run_root): digest_bytes(path.read_bytes())
        for path in run_root.rglob("*")
        if path.is_file()
    }
    assert after == before
