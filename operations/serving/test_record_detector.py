"""The record detector's answer is checked before any stage reads it."""

import math

import pytest

from operations.serving.detector import RecordDetector
from operations.serving.errors import ServingConfigurationError

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def _detector(*detections):
    return RecordDetector(
        run_facts={}, serving_details=None, _detect=lambda _png, _ordinal: list(detections)
    )


def test_a_detection_with_four_finite_corners_is_read():
    [read] = _detector({"corners": SQUARE, "score": 0.5, "class_id": 0}).detect(b"", page_ordinal=1)
    assert read["corners"][2] == [10.0, 10.0] and read["class_name"] == "record"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "1"])
def test_a_corner_that_is_not_a_finite_number_is_refused(value):
    corners = [list(point) for point in SQUARE]
    corners[1][0] = value
    detector = _detector({"corners": corners, "score": 0.5, "class_id": 0})
    with pytest.raises(ServingConfigurationError, match="four \\(x, y\\) corners"):
        detector.detect(b"", page_ordinal=1)
