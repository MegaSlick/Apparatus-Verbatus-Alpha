from operations.corpus.witness_evaluate import witness_reading
from operations.spike_perlector.models import OutputStatus


def test_act_scoped_dai_uses_its_sealed_full_crop_span():
    status, text, reason = witness_reading({"attached": True, "comparable": True, "span": {"start": 0, "end": 4}, "content_health": {"recordable": True, "truncated": False}}, {"outcome": "read", "payload": {"payload": "test"}})
    assert (status, text, reason) == (OutputStatus.COMPLETE, "test", None)


def test_page_witness_without_alignment_span_is_named_unavailable():
    status, text, reason = witness_reading({"attached": True, "comparable": False, "span": None, "content_health": {"recordable": True, "truncated": False}}, {"outcome": "read", "payload": {"payload": "page extra text"}})
    assert (status, text, reason) == (OutputStatus.UNAVAILABLE, None, "not-comparable")


def test_truncated_sealed_excerpt_retains_its_partial_text():
    status, text, reason = witness_reading({"attached": True, "comparable": True, "span": {"start": 1, "end": 3}, "content_health": {"recordable": True, "truncated": True}}, {"outcome": "read", "payload": {"payload": "abcd"}})
    assert (status, text, reason) == (OutputStatus.TRUNCATED, "bc", None)
