"""The weights an arm's preparation fetches: pinned snapshots, the model store's roster
artifacts and the names a manifest may use. Fake downloads only; nothing leaves."""

from __future__ import annotations

import hashlib
import json

import pytest

from operations.bakeoff import weights as W

PAYLOAD, CONFIG = b"weights", b"{}"


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    table = {
        "tiny": {
            "repo": "org/tiny",
            "revision": "a" * 40,
            "files": {
                "model.safetensors": {
                    "size": len(PAYLOAD),
                    "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                },
                "config.json": {
                    "size": len(CONFIG),
                    "git_sha1": hashlib.sha1(b"blob 2\0" + CONFIG).hexdigest(),
                },
            },
        }
    }
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(table))
    monkeypatch.setattr(W, "PINS", path)
    return tmp_path / "store"


def _download(calls, payload=PAYLOAD):
    def download(repo, revision, local_dir):
        calls.append((repo, revision))
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "model.safetensors").write_bytes(payload)
        (local_dir / "config.json").write_bytes(CONFIG)

    return download


def test_a_snapshot_is_fetched_once_checked_and_not_hashed_again(pinned, monkeypatch):
    calls = []
    dest = W.fetch_snapshot(pinned, "tiny", _download(calls))
    assert dest == pinned / "hf" / "tiny" and calls == [("org/tiny", "a" * 40)]
    assert (pinned / "hf" / "tiny.verified.json").is_file()
    monkeypatch.setattr(W, "_digest", lambda *_: pytest.fail("re-hashed a vouched file"))
    W.fetch_snapshot(pinned, "tiny", _download(calls))  # the second arm sharing it
    assert len(calls) == 1


def test_a_snapshot_that_is_not_the_pin_is_refused(pinned):
    with pytest.raises(W.WeightsRefusal, match="model.safetensors"):
        W.fetch_snapshot(pinned, "tiny", _download([], payload=b"weightz"))


def test_roster_artifacts_go_through_the_store_and_its_pinned_digest(tmp_path, monkeypatch):
    from common.chairs import model_store

    roles = []

    def materialize(store, wanted):
        roles.append(wanted)
        return {"selection_complete": True}

    pinned = W.tomllib.loads((W.ROOT / "config" / "models-real.toml").read_text())["chairs"]
    rows = [
        {"artifact": "dai-recordgold-atr", "digest_manifest": pinned["attestator_2"]["digest_manifest"]},
        {"artifact": "yolov26-record-detection", "digest_manifest": "0" * 64},
    ]  # fmt: skip
    monkeypatch.setattr(model_store, "load_download_record", lambda root: {"artifacts": rows})
    W.fetch_roster(tmp_path, ["dai-recordgold-atr"], materialize)
    assert roles == [["attestator_2"]]
    with pytest.raises(W.WeightsRefusal, match="not the pinned"):
        W.fetch_roster(tmp_path, ["yolov26-record-detection"], materialize)


def test_names_and_sizes():
    argv = [".venv/bin/python", "-m", "operations.bakeoff.weights", "fetch", "--store-root",
            "/s", "chandra-ocr-2", "qwen3.5-9b"]  # fmt: skip
    assert W.names_in(argv) == ["chandra-ocr-2", "qwen3.5-9b"]
    assert W.names_in(["python", "-m", "other", "fetch", "x"]) == []
    assert 10e9 < W.size_of("chandra-ocr-2") < 11e9 and 19e9 < W.size_of("qwen3.5-9b") < 20e9
    with pytest.raises(W.WeightsRefusal, match="unknown"):
        W.size_of("nope")
