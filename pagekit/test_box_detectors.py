"""The skew, page-box and content-box detectors together: settings, answers and side effects."""

from __future__ import annotations

import os

from PIL import Image

from pagekit import _box_common as common
from pagekit import _box_synthetic as synth
from pagekit import content, pagebox, skew


def test_every_setting_is_read_by_a_detector_and_every_read_exists():
    names = set(common.load_thresholds())
    read = set(skew._READS) | set(pagebox._READS) | set(content._READS)
    assert read == names


def test_evidence_says_settings_are_unmeasured():
    answer = pagebox.detect_page_box(Image.new("L", (200, 300), 230), (150, 150))
    assert "unmeasured" in answer["evidence"]


def test_detectors_write_no_file(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a detector tried to save an image")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Image.Image, "save", refuse)
    image = synth.turned(synth.page(seed=12), 1.0)
    skew.detect_skew(image, (150, 150))
    pagebox.detect_page_box(image, (150, 150))
    content.detect_content_box(image, (150, 150))
    assert os.listdir(tmp_path) == []
