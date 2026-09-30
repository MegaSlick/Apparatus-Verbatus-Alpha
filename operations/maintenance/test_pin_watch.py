from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from operations.maintenance import pin_watch


class FakeTransport:
    def __init__(self, payloads: Mapping[str, Mapping[str, object]]) -> None:
        self.payloads = payloads
        self.urls: list[str] = []

    def __call__(self, url: str) -> Mapping[str, object]:
        self.urls.append(url)
        value = self.payloads[url]
        return json.loads(json.dumps(value))


def test_pins_come_from_configured_chairs_and_vendor_source_constants() -> None:
    pins = pin_watch._pins(
        (pin_watch.ROOT / "config/models.toml", pin_watch.ROOT / "config/models-real.toml"),
        pin_watch.ROOT / "common/test_vendor_parity.py",
    )
    assert [(pin.repo, pin.revision, pin.host) for pin in pins] == [
        (
            "datalab-to/chandra-ocr-2",
            "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
            "huggingface",
        ),
        (
            "datalab-to/chandra-ocr-2",
            "af93b47dba1b47b6640c86ccf487ed2260ab9a09",
            "huggingface",
        ),
        (
            "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR",
            "e371095d4ffe585f31f4974462931ddbac61ff64",
            "huggingface",
        ),
        (
            "stanford-oval/churro-3B",
            "ca2150ea465d5a3d67818c50e234b9422619c75d",
            "huggingface",
        ),
        (
            "Teklia/YOLOv26-DAI-CReTDHI-Record-Detection",
            "0c57f057391113579e7af170b864542f049e67aa",
            "huggingface",
        ),
        ("Qwen/Qwen3.8-27B", "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0", "huggingface"),
        ("datalab-to/chandra", "d4f7467435aa4137d9539f000ddf0b7ced3eb43f", "github"),
        ("stanford-oval/Churro", "4abb17386d9656199c2776195926545fc527a691", "github"),
    ]


def test_report_marks_unchanged_moved_and_unreachable() -> None:
    unchanged = pin_watch.Pin("owner/unchanged", "a" * 40, "huggingface")
    moved = pin_watch.Pin("owner/moved", "b" * 40, "github")
    unreachable = pin_watch.Pin("owner/missing", "c" * 40, "huggingface")
    transport = FakeTransport(
        {
            "https://huggingface.co/api/models/owner/unchanged": {
                "sha": "a" * 40,
                "lastModified": "2026-09-01T00:00:00.000Z",
            },
            "https://api.github.com/repos/owner/moved/commits/HEAD": {
                "sha": "d" * 40,
                "commit": {"committer": {"date": "2026-09-02T00:00:00Z"}},
            },
        }
    )

    def get_json(url: str) -> Mapping[str, object]:
        if url.endswith("owner/missing"):
            raise OSError("offline")
        return transport(url)

    lines, moved_lines, unreachable_lines = pin_watch.report(
        [unchanged, moved, unreachable], get_json
    )
    assert lines == [
        f"owner/unchanged: unchanged ({'a' * 40})",
        f"owner/moved: upstream-moved (pinned {'b' * 40}, upstream {'d' * 40}, 2026-09-02T00:00:00Z)",
        "owner/missing: unreachable (OSError)",
    ]
    assert moved_lines == [lines[1]]
    assert unreachable_lines == [lines[2]]
    assert transport.urls == [
        "https://huggingface.co/api/models/owner/unchanged",
        "https://api.github.com/repos/owner/moved/commits/HEAD",
    ]


def test_notify_sends_one_decision_for_all_moved_pins(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    pins = [
        pin_watch.Pin("owner/first", "a" * 40, "huggingface"),
        pin_watch.Pin("owner/second", "c" * 40, "huggingface"),
    ]
    monkeypatch.setattr(pin_watch, "ROOT", Path("/repo"))
    monkeypatch.setattr(sys, "argv", ["pin_watch", "--notify"])
    monkeypatch.setattr(pin_watch, "_pins", lambda *_: pins)
    monkeypatch.setattr(
        pin_watch,
        "_get_json",
        lambda _url: {"sha": "b" * 40, "lastModified": "2026-09-01"},
    )
    sent: list[tuple[str, str]] = []

    class Outcome:
        delivered = True

    monkeypatch.setattr(
        pin_watch.client, "send", lambda event, message: sent.append((event, message)) or Outcome()
    )

    assert pin_watch.main() == 0
    assert len(sent) == 1
    assert sent[0][0] == "decision"
    assert "owner/first: upstream-moved" in sent[0][1]
    assert "owner/second: upstream-moved" in sent[0][1]
    assert "upstream-moved" in capsys.readouterr().out


def test_unreadable_configuration_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["pin_watch"])
    monkeypatch.setattr(pin_watch, "_pins", lambda *_: (_ for _ in ()).throw(OSError("missing")))
    assert pin_watch.main() == 1
    assert "could not read pin configuration" in capsys.readouterr().out


def test_an_unreachable_pin_exits_nonzero_and_notifies(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    pins = [
        pin_watch.Pin("owner/current", "a" * 40, "huggingface"),
        pin_watch.Pin("owner/missing", "c" * 40, "huggingface"),
    ]
    monkeypatch.setattr(sys, "argv", ["pin_watch", "--notify"])
    monkeypatch.setattr(pin_watch, "_pins", lambda *_: pins)

    def get_json(url: str) -> Mapping[str, object]:
        if url.endswith("owner/missing"):
            raise TimeoutError("offline")
        return {"sha": "a" * 40, "lastModified": "2026-09-01"}

    monkeypatch.setattr(pin_watch, "_get_json", get_json)
    sent: list[tuple[str, str]] = []

    class Outcome:
        delivered = True

    monkeypatch.setattr(
        pin_watch.client, "send", lambda event, message: sent.append((event, message)) or Outcome()
    )

    assert pin_watch.main() == 1
    assert sent == [
        ("decision", "Vendor pins not checked: owner/missing: unreachable (TimeoutError)")
    ]
    assert "owner/missing: unreachable (TimeoutError)" in capsys.readouterr().out
