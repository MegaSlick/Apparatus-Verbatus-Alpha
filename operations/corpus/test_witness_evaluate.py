from operations.corpus.witness_evaluate import page_health_counts, witness_reading
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


def test_page_health_keeps_true_false_null_missing_and_valid_failed_separate():
    records = [
        {"outcome": "read", "payload": {"chair": "attestator_1", "presented": {"image_sha256": "a"}, "content_health": {"truncated": True}}},
        {"outcome": "failed", "payload": {"chair": "attestator_2", "presented": {"image_sha256": "a"}, "content_health": {"truncated": None}}},
        {"outcome": "read", "payload": {"chair": "attestator_3", "presented": {"image_sha256": "a"}, "content_health": {"truncated": False}}},
    ]
    counts = page_health_counts(records, page_sha256s={"a", "b"}, chairs=("attestator_1", "attestator_2", "attestator_3"))
    assert counts["attestator_1"] == {"missing": 1, "truncated_true": 1, "truncated_false": 0, "truncated_null": 0, "failed_model_response": 0}
    assert counts["attestator_2"]["truncated_null"] == counts["attestator_2"]["failed_model_response"] == 1
    assert counts["attestator_3"]["truncated_false"] == 1
