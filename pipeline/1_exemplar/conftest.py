"""Test fixtures for the Door and Exemplar."""

import json

import pytest


@pytest.fixture
def empty_triage_manifest(tmp_path):
    from door import triage_manifest

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {"schema": triage_manifest.MANIFEST_SCHEMA, "corpus_id": "parish-a", "records": []}
        ),
        encoding="utf-8",
    )
    return path
