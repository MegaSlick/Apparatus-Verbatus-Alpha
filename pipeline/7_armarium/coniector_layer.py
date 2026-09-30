"""The Coniector's layer in an Armarium package: each reconstruction beneath its diplomatic.

A reconstruction is labelled, unconfirmed and never an act. It is shown only
beneath a delivered act (and a join only when every piece is delivered), with
who made it (a model chair, or a person), its departures and its findings,
which are flags and change nothing. One that was not made says why, and the
diplomatic reading above it stands as delivered.

`coniector.jsonl` carries one row per shown reconstruction when the JSONL
format is selected; the text bundle carries the same beneath each act, and each
join as its own `## JOIN RECONSTRUCTION ... (not an act)` section. A clean machine
recomputes every made reconstruction from the row's own diplomatic pieces and
departures, and holds each piece to the delivered literal of its act.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.reading_annotations import read_doubt_marks
from common.reconstruction_records import LABEL, MAKER_FIELDS, MAKER_MODEL, MAKER_PERSON

CONIECTOR_MEMBER: Final = "coniector.jsonl"
ROW_SCHEMA: Final = "armarium-coniector-reconstruction.v1"
ROW_FIELDS: Final = frozenset(
    {
        "schema",
        "unit",
        "act_ids",
        "act_keys",
        "label",
        "made",
        "maker",
        "diplomatic_raw_pieces",
        "reconstruction_raw",
        "reconstruction_text",
        "reconstruction_uncertainty",
        "continues",
        "departures",
        "flags",
        "not_made",
        "record_ref",
    }
)
JOIN_SECTION_PREFIX: Final = "## JOIN RECONSTRUCTION "
ROW_LINE: Final = "reconstruction_row:"
JOIN_SECTION_SUFFIX: Final = " (not an act)"


def _sha256(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def export_rows(
    records: Sequence[Mapping[str, Any]],
    diplomatic_raw: Mapping[str, str],
    delivered_texts: Mapping[str, str],
    record_refs: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    """The rows shown: each verified Coniector record whose acts are all delivered.

    `diplomatic_raw` is each act's diplomatic reading with its doubt marks, as the
    Coniector was shown it; `delivered_texts` each delivered act's literal; a
    record's pieces must be those literals exactly, or the reconstruction would
    stand beneath a text other than the one delivered.
    """
    rows = []
    for record in records:
        act_ids = list(record["act_ids"])
        if not all(act_id in delivered_texts for act_id in act_ids):
            continue
        for act_id, clean_sha256 in zip(act_ids, record["diplomatic_clean_sha256s"], strict=True):
            if _sha256(delivered_texts[act_id]) != clean_sha256:
                raise SchemaRefusal(
                    f"the reconstruction beneath {act_id} was made over a reading other than the "
                    "one delivered"
                )
        rows.append(
            {
                "schema": ROW_SCHEMA,
                "unit": record["unit"],
                "act_ids": act_ids,
                "act_keys": list(record["act_keys"]),
                "label": record["label"],
                "made": record["made"],
                "maker": dict(record["maker"]),
                "diplomatic_raw_pieces": [diplomatic_raw[act_id] for act_id in act_ids],
                "reconstruction_raw": record["reconstruction_raw"],
                "reconstruction_text": record["reconstruction_text"],
                "reconstruction_uncertainty": record["reconstruction_uncertainty"],
                "continues": record["continues"],
                "departures": [dict(departure) for departure in record["departures"]],
                "flags": [dict(finding) for finding in record["findings"]],
                "not_made": [dict(reason) for reason in record["not_made"]],
                "record_ref": dict(record_refs[record["act_ids"][0]]),
            }
        )
    return sorted(rows, key=lambda row: (row["unit"], row["act_keys"]))


def maker_line(maker: Mapping[str, Any]) -> str:
    """Who made a reconstruction, in one line."""
    if maker["kind"] == MAKER_PERSON:
        return "a person"
    identity = maker.get("resolved_identity") or {}
    revision = (maker.get("resolved_revision") or {}).get("value")
    name = identity.get("repo") or identity.get("path") or "no model"
    return f"model, chair {maker['chair']} ({name}@{revision})"


def _flag_text(flag: Mapping[str, Any]) -> str:
    reason = flag.get("reason")
    return flag["code"] + (f" ({reason})" if reason else "")


def reconstruction_lines(row: Mapping[str, Any]) -> list[str]:
    """The lines a text bundle shows for one reconstruction, never a recognised act field."""
    lines = [
        f"reconstruction_label: {row['label']}",
        f"reconstruction_maker: {maker_line(row['maker'])}",
    ]
    if row["flags"]:
        lines.append(
            "reconstruction_flags: " + "; ".join(_flag_text(flag) for flag in row["flags"])
        )
    if row["made"]:
        lines += [
            "reconstruction_text:",
            json.dumps(row["reconstruction_text"], ensure_ascii=False),
            "reconstruction_departures:",
            json.dumps(
                [
                    {
                        key: departure[key]
                        for key in ("diplomatic", "reconstruction", "reason")
                        if departure.get(key) is not None
                    }
                    for departure in row["departures"]
                ],
                ensure_ascii=False,
            ),
        ]
    else:
        reasons = "; ".join(f"{reason['code']}: {reason['detail']}" for reason in row["not_made"])
        lines.append(f"reconstruction: not made: {reasons}")
    # The whole row, so the text bundle recomputes on its own when it is the only format.
    lines += [ROW_LINE, json.dumps(row, ensure_ascii=False, sort_keys=True)]
    return lines


def join_section(row: Mapping[str, Any]) -> list[str]:
    """A join's own text-bundle section, headed as no act is."""
    return [
        f"{JOIN_SECTION_PREFIX}{' + '.join(row['act_keys'])}{JOIN_SECTION_SUFFIX}",
        "reconstruction_pieces: "
        + " + ".join(
            f"{key} ({act_id})" for key, act_id in zip(row["act_keys"], row["act_ids"], strict=True)
        ),
        *reconstruction_lines(row),
        "",
    ]


def _splice(base: str, departures: Sequence[Mapping[str, Any]]) -> str:
    pieces, cursor = [], 0
    for departure in departures:
        span = departure.get("raw_span")
        if not isinstance(span, Mapping):
            raise SchemaRefusal("a reconstruction departure has no raw span")
        start, end = span.get("start"), span.get("end")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not cursor <= start <= end <= len(base)
            or base[start:end] != departure.get("diplomatic")
            or not isinstance(departure.get("reconstruction"), str)
        ):
            raise SchemaRefusal("a reconstruction departure does not quote its diplomatic span")
        pieces += [base[cursor:start], departure["reconstruction"]]
        cursor = end
    pieces.append(base[cursor:])
    return "".join(pieces)


def verify_row(row: Any, literals: Mapping[str, tuple]) -> dict[str, Any]:
    """One `coniector.jsonl` row, recomputed; `literals` maps each delivered act to its literal."""
    if not isinstance(row, dict) or set(row) != ROW_FIELDS or row["schema"] != ROW_SCHEMA:
        raise SchemaRefusal(f"a {CONIECTOR_MEMBER} row is not a {ROW_SCHEMA} row")
    if row["label"] != LABEL or row["unit"] not in ("act", "join"):
        raise SchemaRefusal(f"a {CONIECTOR_MEMBER} row is not labelled as a reconstruction")
    maker = row["maker"]
    if (
        not isinstance(maker, dict)
        or set(maker) != MAKER_FIELDS
        or maker["kind"]
        not in (
            MAKER_MODEL,
            MAKER_PERSON,
        )
    ):
        raise SchemaRefusal(f"a {CONIECTOR_MEMBER} row does not say who made it")
    act_ids, pieces = row["act_ids"], row["diplomatic_raw_pieces"]
    if (
        not isinstance(act_ids, list)
        or not isinstance(pieces, list)
        or len(act_ids) != len(pieces)
        or (len(act_ids) == 1) != (row["unit"] == "act")
        or not act_ids
    ):
        raise SchemaRefusal(f"a {CONIECTOR_MEMBER} row does not name its pieces")
    for act_id, piece in zip(act_ids, pieces, strict=True):
        if act_id not in literals or not isinstance(piece, str):
            raise SchemaRefusal(f"a reconstruction stands beneath {act_id}, which is not delivered")
        if read_doubt_marks(piece)[0] != literals[act_id][0]:
            raise SchemaRefusal(
                f"the reconstruction beneath {act_id} departs from a text other than its literal"
            )
    if row["made"] is True:
        text = _splice("\n".join(pieces), row["departures"])
        clean, uncertainty = read_doubt_marks(text)
        if (
            text != row["reconstruction_raw"]
            or clean != row["reconstruction_text"]
            or uncertainty != row["reconstruction_uncertainty"]
            or row["not_made"]
        ):
            raise SchemaRefusal("a made reconstruction is not its departures applied to its pieces")
    elif row["made"] is False:
        if (
            row["reconstruction_raw"] is not None
            or row["reconstruction_text"] is not None
            or row["departures"]
            or not row["not_made"]
        ):
            raise SchemaRefusal("a reconstruction not made carries a text or no reason")
    else:
        raise SchemaRefusal(f"a {CONIECTOR_MEMBER} row does not say whether it was made")
    return row


def text_bundle_rows(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Each reconstruction row a text bundle carries, its readable lines held to it."""
    rows = []
    for index, line in enumerate(lines):
        if line != ROW_LINE:
            continue
        try:
            row = json.loads(lines[index + 1])
        except (IndexError, ValueError) as error:
            raise SchemaRefusal("a text-bundle reconstruction row is not JSON") from error
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise SchemaRefusal(f"a text-bundle reconstruction row is not a {ROW_SCHEMA} row")
        expected = reconstruction_lines(row)
        start = index + 2 - len(expected)
        if start < 0 or list(lines[start : index + 2]) != expected:
            raise SchemaRefusal("a text bundle's reconstruction lines do not say what its row says")
        rows.append(row)
    return rows
