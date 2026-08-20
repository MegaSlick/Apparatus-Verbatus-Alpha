"""The PDF target is run configuration; safety bounds remain renderer code."""

import importlib.util
import sys
from io import BytesIO
from pathlib import Path

import door
import pdf_render
import pytest
import render_config
from PIL import Image
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
    """300 since 2026-08-05. The value is a decision, not an accident, so it is
    asserted here — and `config/pdf_render.toml` carries the measurement it came
    from. A change to one without the other should fail."""
    settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    assert settings.to_record() == {
        "configured_target_dpi": 300,
        "target_dpi": 300,
        "minimum_dpi": 72,
    }


def test_the_target_is_configuration_rather_than_a_constant_in_code(tmp_path):
    """Ruling 14's actual requirement: changing this needs no code change.

    Asserted both ways. The loader carries no shipped value of its own — a constant
    there would keep working after somebody edited the file and would be very hard
    to notice — and a config naming a different target really does change the run.
    """
    source = (Path(__file__).resolve().parent / "render_config.py").read_text(encoding="utf-8")
    assert "300" not in source, "the shipped target leaked into the loader as a constant"

    for chosen in (150, 300, 600):
        configured = tmp_path / f"render-{chosen}.toml"
        configured.write_text(f"[pdf]\ntarget_dpi = {chosen}\n", encoding="utf-8")
        settings = render_config.load_pdf_render_settings(configured, minimum_dpi=72)
        assert settings.configured_target_dpi == settings.target_dpi == chosen


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


@pytest.mark.parametrize(("mode", "value"), [("I", 70_000), ("F", 1_000.25)])
def test_exemplar_accepts_the_lossless_tiff_contract_for_high_precision_fanned_pages(mode, value):
    """The Door and Exemplar agree that these samples are TIFF, not clipped RGB."""
    output = BytesIO()
    Image.new(mode, (2, 2), 1 if mode == "I" else 0.5).save(
        output,
        format="TIFF",
        save_all=True,
        append_images=[Image.new(mode, (2, 2), value)],
    )
    rendered, geometry, contract = door.render_raster_page(output.getvalue(), 1)

    assert contract["output"]["codec"] == "tiff"
    assert (
        door.admission.inspect_source(
            rendered,
            declared_sha256=None,
            policy=door.admission.load_format_policy(),
        ).outcome
        == "admitted"
    )
    _exemplar_module()._verify_render_contract(
        contract,
        1,
        {"geometry": {"width": geometry.width, "height": geometry.height}},
        {},
        container_format="tiff",
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


def test_the_policy_is_read_once_so_its_digest_is_of_the_bytes_that_were_parsed(tmp_path):
    """`config_sha256` names the file as read, not the settings as resolved."""
    configured = tmp_path / "render.toml"
    configured.write_bytes(b"[pdf]\ntarget_dpi = 240\n")
    binding = render_config.load_pdf_render_binding(configured, minimum_dpi=72)

    assert binding.settings.configured_target_dpi == 240
    assert binding.config_sha256 == digest_bytes(configured.read_bytes())
    # The CLI override moves the target without moving the file: the digest still
    # answers "which pdf_render.toml did this run parse", which is the question a
    # point-of-use recheck asks.
    overridden = render_config.load_pdf_render_binding(
        configured, target_override=600, minimum_dpi=72
    )
    assert overridden.settings.configured_target_dpi == 600
    assert overridden.config_sha256 == binding.config_sha256


def test_a_render_policy_rewritten_while_the_door_binds_cannot_split_the_run(tmp_path, monkeypatch):
    """The recorded settings and the sealed digest can no longer disagree.

    The door used to resolve `PdfRenderSettings` from this file and then let
    `run_config_bindings` open it a second time for the digest. A rewrite landing
    between those reads produced a run that exited 0 while `run.json` recorded one
    target DPI and its `config_digest` bound the bytes of another — a proof run
    claiming a configuration it did not execute (audit S6, reproduced at a
    one-DPI edit).

    The rewrite here lands at exactly that instant: the moment the one read
    returns. There is no second read for it to reach, so the run records the
    target it rendered with, seals the digest of the bytes that target came from,
    and leaves the changed file to be refused as an incompatible reuse next time.
    """
    from common.chairs.registry import ChairRegistry
    from common.stage import load_fixture, run_config_bindings

    run_root = tmp_path / "runs"
    config_path = tmp_path / "pdf_render.toml"
    original = (ROOT / "config" / "pdf_render.toml").read_bytes()
    config_path.write_bytes(original)
    text = original.decode("utf-8")
    assert "target_dpi = 300" in text, "the shipped render config no longer targets 300 DPI"
    raced = text.replace("target_dpi = 300", "target_dpi = 301")
    read_once = render_config.load_pdf_render_binding

    def rewriting_loader(path, **kwargs):
        binding = read_once(path, **kwargs)
        config_path.write_text(raced, encoding="utf-8")
        return binding

    monkeypatch.setattr(door.render_config, "load_pdf_render_binding", rewriting_loader)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "door.py",
            "--run-root",
            str(run_root),
            "--run-id",
            "raced-render",
            "--fixture-root",
            str(ROOT / "proof"),
            "--pdf-render-config",
            str(config_path),
        ],
    )
    assert door.main() == 0
    assert config_path.read_bytes() != original, "the rewrite this test is about did not happen"

    run = RunTree(run_root, "raced-render").read_run()
    assert run["render_settings"]["pdf"]["configured_target_dpi"] == 300
    # The whole claim in one comparison: the run's configuration digest is the one
    # taken over the bytes it recorded rendering under, not over the bytes that
    # replaced them mid-binding.
    expected = run_config_bindings(
        ChairRegistry.from_toml(str(ROOT / "config" / "models.toml")).config,
        load_fixture(str(ROOT / "proof")),
        "happy",
        pdf_render_config_sha256=digest_bytes(original),
    )
    assert run["config_digest"] == expected["config_digest"]
    assert run["sealed_config_digests"] == expected["sealed_config_digests"]
    assert run["sealed_config_digests"]["pdf-render"] == digest_bytes(original)


def test_both_door_entry_points_seal_the_settings_they_actually_parsed():
    """Neither route may fall back to an unbound read of the render policy.

    Asserted on the source because the fallback it forbids is a default argument:
    `process_sources` and `decide` will still load the policy themselves when no
    settings are supplied, which is a convenience for direct callers and would be
    an unsealed second read on a production path. Both production paths pass the
    settings the run sealed, and this is what says so.
    """
    import ast

    module = ast.parse((Path(__file__).resolve().parent / "door.py").read_text(encoding="utf-8"))
    entry_points = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"fixture_submission", "real_submission"}
    }
    assert set(entry_points) == {"fixture_submission", "real_submission"}
    for name, node in entry_points.items():
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "process_sources"
        ]
        assert len(calls) == 1, f"{name} does not admit its sources exactly once"
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
        assert keywords.get("pdf_settings") == "pdf_settings", (
            f"{name} lets process_sources fall back to its own unbound read of the render policy"
        )
        requires = [
            ast.unparse(call)
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and "require_sealed_config" in ast.unparse(call.func)
        ]
        assert "context.require_sealed_config('pdf-render', pdf_render_binding.config_sha256)" in (
            requires
        ), f"{name} never proves its render settings against the digest the run sealed"
