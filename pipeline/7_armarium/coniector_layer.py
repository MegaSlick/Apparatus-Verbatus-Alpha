"""The Coniector's layer in an Armarium package: each reconstruction beneath its diplomatic.

A reconstruction is labelled, unconfirmed and never an act. It is shown only
beneath a delivered act (and a join only when every piece is delivered), with
who made it (a model chair, or a person), its departures and its findings,
which are flags and change nothing. One that was not made says why, and the
diplomatic reading above it stands as delivered.

The Coniector reconstructs from the model's reading. Beneath an act a person
corrected, the row is held to the model's reading, shown above it as "model
reading (original)", and says so in `made_from`, so it never reads as a
reconstruction of the person's text.

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
from common.correction import ORIGINAL_LABEL
from common.reading_annotations import bracket_doubt_marks, read_doubt_marks
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
# Present only on a row with a piece a person corrected: what it was made from.
MADE_FROM_FIELD: Final = "made_from"
JOIN_SECTION_PREFIX: Final = "## JOIN RECONSTRUCTION "
ROW_LINE: Final = "reconstruction_row:"
JOIN_SECTION_SUFFIX: Final = " (not an act)"


def anchor_act(row: Mapping[str, Any]) -> str:
    """The act a row is shown beneath: its act, or a join's first piece."""
    return row["act_ids"][0]


def _sha256(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def export_rows(
    records: Sequence[Mapping[str, Any]],
    diplomatic_raw: Mapping[str, str],
    delivered_texts: Mapping[str, str],
    record_refs: Mapping[str, Mapping[str, str]],
    corrected: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The rows shown: each verified Coniector record whose acts are all delivered.

    `diplomatic_raw` is each act's diplomatic reading with its doubt marks, as the
    Coniector was shown it; `delivered_texts` each delivered act's model reading:
    its literal, or for an act in `corrected` the model's reading a person
    corrected. A record's pieces must be those texts exactly, or the
    reconstruction would stand beneath a reading other than the one it was
    made from; a row with a corrected piece names `made_from`.
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
        made_from = (
            {MADE_FROM_FIELD: ORIGINAL_LABEL} if any(a in corrected for a in act_ids) else {}
        )
        rows.append(
            {
                **made_from,
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


def maker_text(maker: Mapping[str, Any]) -> str:
    """Who made a reconstruction, in words."""
    if maker["kind"] == MAKER_PERSON:
        return "a person"
    identity = maker.get("resolved_identity") or {}
    revision = (maker.get("resolved_revision") or {}).get("value")
    name = identity.get("repo") or identity.get("path") or "no model"
    return f"model, chair {maker['chair']} ({name}@{revision})"


RECONSTRUCTION_OPEN: Final = "⟨"
RECONSTRUCTION_CLOSE: Final = "⟩"


def with_reconstructions(row: Mapping[str, Any]) -> str:
    """The "with reconstructions" view of a made row: each departure as `⟨word⟩`.

    The row's diplomatic pieces with every departure spliced in between angle
    brackets, and every doubt mark left shown as a reader is shown it
    (`[illegible]`, `[reading?]`). It is only ever shown inside the row's labelled
    block, never as the established text.
    """
    if not all(isinstance(departure.get("reconstruction"), str) for departure in row["departures"]):
        raise SchemaRefusal("a reconstruction departure does not name its reconstruction")
    marked = _splice(
        "\n".join(row["diplomatic_raw_pieces"]),
        [
            {
                **departure,
                "reconstruction": RECONSTRUCTION_OPEN
                + departure["reconstruction"]
                + RECONSTRUCTION_CLOSE,
            }
            for departure in row["departures"]
        ],
    )
    return bracket_doubt_marks(marked)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def reconstruction_lines(row: Mapping[str, Any]) -> list[str]:
    """The lines a text bundle shows for one reconstruction.

    Every value a model or a person wrote is one JSON line, so none can start a
    line the act parser reads; the block ends with the whole row, so the text
    bundle recomputes on its own.
    """
    lines = [
        f"reconstruction_label: {LABEL}",
        *([f"reconstruction_made_from: {row[MADE_FROM_FIELD]}"] if MADE_FROM_FIELD in row else []),
        "reconstruction_maker:",
        _json(maker_text(row["maker"])),
    ]
    if row["flags"]:
        lines += ["reconstruction_flags:", _json(row["flags"])]
    if row["made"]:
        lines += [
            "reconstruction_text:",
            _json(row["reconstruction_text"]),
            "with_reconstructions:",
            _json(with_reconstructions(row)),
            "reconstruction_departures:",
            _json(
                [
                    {
                        key: departure[key]
                        for key in ("diplomatic", "reconstruction", "reason")
                        if departure.get(key) is not None
                    }
                    for departure in row["departures"]
                ]
            ),
        ]
    else:
        lines += ["reconstruction_not_made:", _json(row["not_made"])]
    lines += [ROW_LINE, _json(row)]
    return lines


def _join_heading(row: Mapping[str, Any]) -> str:
    return f"{JOIN_SECTION_PREFIX}{' + '.join(row['act_keys'])}{JOIN_SECTION_SUFFIX}"


def join_section(row: Mapping[str, Any]) -> list[str]:
    """A join's own text-bundle section, headed as no act is."""
    return [
        _join_heading(row),
        "reconstruction_pieces:",
        _json(
            [
                {"act_key": key, "act_id": act_id}
                for key, act_id in zip(row["act_keys"], row["act_ids"], strict=True)
            ]
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


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _objects(value: Any, required: frozenset[str], optional: frozenset[str] = frozenset()) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and required <= set(item) <= required | optional
        and all(isinstance(item[key], str) for key in required | (set(item) & optional))
        for item in value
    )


def _require_shape(row: Any) -> dict[str, Any]:
    """Refuse a row that is not the closed shape, before anything reads a field of it."""
    if (
        not isinstance(row, dict)
        or set(row) - {MADE_FROM_FIELD} != ROW_FIELDS
        or row["schema"] != ROW_SCHEMA
        or row.get(MADE_FROM_FIELD, ORIGINAL_LABEL) != ORIGINAL_LABEL
    ):
        raise SchemaRefusal(f"a reconstruction row is not a {ROW_SCHEMA} row")
    if row["label"] != LABEL or row["unit"] not in ("act", "join"):
        raise SchemaRefusal("a reconstruction row is not labelled as a reconstruction")
    maker = row["maker"]
    if (
        not isinstance(maker, dict)
        or set(maker) != MAKER_FIELDS
        or maker["kind"] not in (MAKER_MODEL, MAKER_PERSON)
        or not isinstance(maker["chair"], str)
        or not isinstance(maker["resolved_identity"], (dict, type(None)))
        or not isinstance(maker["resolved_revision"], (dict, type(None)))
    ):
        raise SchemaRefusal("a reconstruction row does not say who made it")
    act_ids, keys, pieces = row["act_ids"], row["act_keys"], row["diplomatic_raw_pieces"]
    if (
        not _is_string_list(act_ids)
        or not _is_string_list(keys)
        or not _is_string_list(pieces)
        or not act_ids
        or not len(act_ids) == len(keys) == len(pieces)
        or (len(act_ids) == 1) != (row["unit"] == "act")
    ):
        raise SchemaRefusal("a reconstruction row does not name its pieces")
    continues = row["continues"]
    if (row["unit"] == "act" and continues is not None) or (
        row["unit"] == "join" and not isinstance(continues, bool)
    ):
        raise SchemaRefusal("a reconstruction row's continuation does not fit its unit")
    if (
        not isinstance(row["made"], bool)
        or not isinstance(row["departures"], list)
        or not all(isinstance(item, dict) for item in row["departures"])
        or not _objects(row["flags"], frozenset({"code"}), frozenset({"reason"}))
        or not isinstance(row["not_made"], list)
        or not all(
            isinstance(item, dict)
            and isinstance(item.get("code"), str)
            and isinstance(item.get("detail"), str)
            for item in row["not_made"]
        )
        or not isinstance(row["record_ref"], dict)
    ):
        raise SchemaRefusal("a reconstruction row's fields are not their closed shapes")
    return row


def verify_row(
    row: Any,
    literals: Mapping[str, tuple],
    keys: Mapping[str, str],
    corrected: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One reconstruction row, recomputed.

    `literals` maps each delivered act to its literal and `keys` each act to its
    key, both read from the same format the row came from; `corrected` maps
    each act a person corrected to the model's reading that format shows beside
    it, which the row's pieces are held to instead, and a row names
    `made_from` exactly when it has such a piece.
    """
    row = _require_shape(row)
    corrected = corrected or {}
    if (MADE_FROM_FIELD in row) != any(act_id in corrected for act_id in row["act_ids"]):
        raise SchemaRefusal(
            "a reconstruction row does not say it was made from the model's reading exactly "
            "when a piece of it is a reading a person corrected"
        )
    literals = {
        act_id: ((corrected[act_id],) if act_id in corrected else literal)
        for act_id, literal in literals.items()
    }
    for act_id, key, piece in zip(
        row["act_ids"], row["act_keys"], row["diplomatic_raw_pieces"], strict=True
    ):
        if act_id not in literals:
            raise SchemaRefusal(f"a reconstruction stands beneath {act_id}, which is not delivered")
        if keys.get(act_id) != key:
            raise SchemaRefusal(f"a reconstruction names {act_id} by another act's key")
        if read_doubt_marks(piece)[0] != literals[act_id][0]:
            raise SchemaRefusal(
                f"the reconstruction beneath {act_id} departs from a text other than its literal"
            )
    if row["made"]:
        text = _splice("\n".join(row["diplomatic_raw_pieces"]), row["departures"])
        clean, uncertainty = read_doubt_marks(text)
        if (
            text != row["reconstruction_raw"]
            or clean != row["reconstruction_text"]
            or uncertainty != row["reconstruction_uncertainty"]
            or row["not_made"]
        ):
            raise SchemaRefusal("a made reconstruction is not its departures applied to its pieces")
    elif (
        row["reconstruction_raw"] is not None
        or row["reconstruction_text"] is not None
        or row["reconstruction_uncertainty"] is not None
        or row["departures"]
        or not row["not_made"]
    ):
        raise SchemaRefusal("a reconstruction not made carries a text or no reason")
    return row


def text_bundle_placements(
    lines: Sequence[str],
) -> tuple[set[str], list[tuple[str, dict[str, Any]]]]:
    """`(acts sectioned in this file, [(place, row)])` for one text-bundle file.

    A row's place is the act section it follows (`act:<act_id>`) or its own join
    section (`join`); each block must be exactly what its row renders, so no
    line of it says anything its row does not.
    """
    acts: set[str] = set()
    placed: list[tuple[str, dict[str, Any]]] = []
    current: str | None = None
    join_start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("act-id: "):
            current = "act:" + line.removeprefix("act-id: ")
            acts.add(current.removeprefix("act:"))
            join_start = None
        elif line.startswith(JOIN_SECTION_PREFIX):
            current, join_start = "join", index
        elif line.startswith("## "):
            current, join_start = None, None
        elif line == ROW_LINE:
            try:
                row = _require_shape(json.loads(lines[index + 1]))
            except (IndexError, ValueError) as error:
                raise SchemaRefusal("a text-bundle reconstruction row is not JSON") from error
            expected = reconstruction_lines(row)
            start = index + 2 - len(expected)
            if current is None or start < 0 or list(lines[start : index + 2]) != expected:
                raise SchemaRefusal(
                    "a text bundle's reconstruction lines do not say what its row says"
                )
            if current == "join":
                section = join_section(row)
                if (
                    row["unit"] != "join"
                    or list(lines[join_start : join_start + len(section)]) != section
                ):
                    raise SchemaRefusal("a text-bundle join section is not its row's")
            elif row["unit"] != "act" or current != "act:" + row["act_ids"][0]:
                raise SchemaRefusal("a reconstruction stands beneath another act's section")
            placed.append((current, row))
    return acts, placed
