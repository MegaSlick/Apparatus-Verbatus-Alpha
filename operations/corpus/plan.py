"""The fetch plan: `record_url` parsed once, rows grouped by the page they share.

RecordGold's `record_url` is a IIIF Image API 2 crop request, e.g.

    https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/
    239,208,1232,443/full/0/default.jpg

    host           europe.iiif.teklia.com
    identifier     geneanet/Ardennes_BMS/380403/00026.jpg   (percent-decoded)
    region         x,y,w,h in full-page pixels
    size           full          <- what we ask for; anything else is unrecognised
    rotation       0             <- anything else means the boxes are in a
                                     different frame from the pixels we would fetch
    quality        default
    format         jpg

Every field but the region is a closed vocabulary of one accepted value, and this
parser refuses anything else **by name** rather than normalising it — `SPEC.md`
§6 records this as unverified territory ("the exact `record_url` region semantics
beyond the one example"), and the measured snapshot proves the caution earned its
keep: 40 of 7,720 rows across `val`/`train` carry `rotation=180`, which this parser
refuses rather than silently fetching an image whose boxes would not line up with
its pixels.

Rows are grouped by `identifier`, not by the parquet's `source` column — `source`
names the collection (`Ardennes`, `Tours`, `Ile de Ré`) but two collections
(`Tours`, `Ile de Ré`) share one IIIF prefix (`dai-cretdhi`), so only the full
identifier path is a page. The identifier's last `/`-segment is the page's own
filename (`designation`); everything before it is the volume path. Those two,
joined with the row's `source`, feed `common.contracts.identities.physical_page_id`
— the `pac_`-ladder anchor `SPEC.md` §5.3(c) requires, because a RecordGold box
must never be minted as an `act_*` identity (those bind *originally minted*
bounds; the Designator will never mint this exact rectangle).

`identifier_encoded` — the raw, already-percent-escaped path segment as it appears
in `record_url` — is threaded through unchanged into `info_url` and every
`image_url` candidate. Round-tripping through `unquote`/`quote` was tried and
rejected: RecordGold identifiers contain literal `+` (e.g. `img+LaCouarde`), which
`quote` re-escapes as `%2B` where the original left it bare — of the 7,720
measured rows, 3,643 would come back byte-different from a round trip. Reusing the
exact substring the server itself produced is the only way to guarantee the URL
this plan hands to the fetcher is the one that actually worked.
"""

import urllib.parse
from re import compile as _compile
from typing import Any, NamedTuple

from common.contracts.canonical import self_hash, verify_self_hash
from common.contracts.identities import physical_act_id, physical_page_id

from . import CorpusRefusal
from .rows import CORPUS_ID, SPLITS

SCHEMA = "recordgold-fetch-plan.v1"

EXPECTED_HOST = "europe.iiif.teklia.com"
EXPECTED_SIZE = "full"
EXPECTED_ROTATION = "0"
EXPECTED_QUALITY = "default"
EXPECTED_FORMAT = "jpg"

# https://<host>/iiif/2/<identifier>/<x>,<y>,<w>,<h>/<size>/<rotation>/<quality>.<format>
_URL_RE = _compile(
    r"^https://(?P<host>[^/]+)/iiif/2/(?P<identifier>.+)/"
    r"(?P<x>-?\d+),(?P<y>-?\d+),(?P<w>\d+),(?P<h>\d+)/"
    r"(?P<size>[^/]+)/(?P<rotation>[^/]+)/(?P<quality>[^./]+)\.(?P<format>[A-Za-z0-9]+)$"
)

PLAN_REFUSAL_REASONS = frozenset(
    {
        "unparseable-record-url",
        "unexpected-host",
        "unsupported-size-parameter",
        "unsupported-rotation-parameter",
        "unsupported-quality-parameter",
        "unsupported-format-parameter",
        "non-positive-region",
        "inconsistent-source-for-identifier",
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "self-hash-mismatch",
    }
)


class ParsedRecordUrl(NamedTuple):
    identifier: str
    """Percent-decoded, human-readable, the grouping key for a page."""

    identifier_encoded: str
    """The raw escaped path segment exactly as `record_url` carried it."""

    host: str
    region: dict[str, int]


def parse_record_url(record_url: Any) -> ParsedRecordUrl:
    """Parse one `record_url`, refusing anything this parser does not recognise."""
    if not isinstance(record_url, str):
        raise CorpusRefusal(
            f"unparseable-record-url: record_url must be a string, got {record_url!r}"
        )
    match = _URL_RE.match(record_url)
    if match is None:
        raise CorpusRefusal(
            f"unparseable-record-url: {record_url!r} does not match the IIIF Image "
            "API 2 crop shape this parser recognises"
        )
    host = match.group("host")
    if host != EXPECTED_HOST:
        raise CorpusRefusal(
            f"unexpected-host: {host!r} in {record_url!r}, expected {EXPECTED_HOST!r}"
        )

    size = match.group("size")
    if size != EXPECTED_SIZE:
        raise CorpusRefusal(
            f"unsupported-size-parameter: {size!r} in {record_url!r}, only {EXPECTED_SIZE!r} is recognised"
        )
    rotation = match.group("rotation")
    if rotation != EXPECTED_ROTATION:
        raise CorpusRefusal(
            f"unsupported-rotation-parameter: {rotation!r} in {record_url!r}, only "
            f"{EXPECTED_ROTATION!r} is recognised — a non-zero rotation would put the "
            "region's x,y,w,h in a different frame from the fetched pixels"
        )
    quality = match.group("quality")
    if quality != EXPECTED_QUALITY:
        raise CorpusRefusal(
            f"unsupported-quality-parameter: {quality!r} in {record_url!r}, only "
            f"{EXPECTED_QUALITY!r} is recognised"
        )
    fmt = match.group("format")
    if fmt != EXPECTED_FORMAT:
        raise CorpusRefusal(
            f"unsupported-format-parameter: {fmt!r} in {record_url!r}, only {EXPECTED_FORMAT!r} is recognised"
        )

    x, y, w, h = (int(match.group(name)) for name in ("x", "y", "w", "h"))
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise CorpusRefusal(f"non-positive-region: x={x} y={y} w={w} h={h} in {record_url!r}")

    identifier_encoded = match.group("identifier")
    identifier = urllib.parse.unquote(identifier_encoded)
    if not identifier or "/" not in identifier:
        raise CorpusRefusal(
            f"unparseable-record-url: identifier {identifier!r} carries no volume/page "
            f"structure in {record_url!r}"
        )
    return ParsedRecordUrl(
        identifier=identifier,
        identifier_encoded=identifier_encoded,
        host=host,
        region={"x": x, "y": y, "w": w, "h": h},
    )


def _volume_and_designation(identifier: str) -> tuple[str, str]:
    segments = identifier.split("/")
    return "/".join(segments[:-1]), segments[-1]


def _image_urls(host: str, identifier_encoded: str) -> dict[str, str]:
    base = f"https://{host}/iiif/2/{identifier_encoded}"
    return {
        "full": f"{base}/full/{EXPECTED_SIZE}/{EXPECTED_ROTATION}/{EXPECTED_QUALITY}.{EXPECTED_FORMAT}",
        "max": f"{base}/full/max/{EXPECTED_ROTATION}/{EXPECTED_QUALITY}.{EXPECTED_FORMAT}",
    }


def _measure(pages: list[dict[str, Any]], refusals: list[dict[str, Any]]) -> dict[str, Any]:
    pages_per_split: dict[str, int] = {split: 0 for split in sorted(SPLITS)}
    records_per_page: dict[str, int] = {}
    cross_split_page_count = 0
    for page in pages:
        for split in page["splits_present"]:
            pages_per_split[split] += 1
        if len(page["splits_present"]) > 1:
            cross_split_page_count += 1
        count = str(len(page["records"]))
        records_per_page[count] = records_per_page.get(count, 0) + 1
    refused_count_by_reason: dict[str, int] = {}
    for refusal in refusals:
        reason = refusal["reason"]
        refused_count_by_reason[reason] = refused_count_by_reason.get(reason, 0) + 1
    return {
        "distinct_pages_total": len(pages),
        "pages_per_split": pages_per_split,
        "records_per_page_distribution": dict(
            sorted(records_per_page.items(), key=lambda kv: int(kv[0]))
        ),
        "cross_split_page_count": cross_split_page_count,
        "refused_row_count": len(refusals),
        "refused_count_by_reason": dict(sorted(refused_count_by_reason.items())),
    }


def build_fetch_plan(
    rows: list[dict[str, Any]], source_row_snapshot_self_hash: str
) -> dict[str, Any]:
    """Group `rows` (row-snapshot dicts) into one page per IIIF identifier.

    Refuses a row's `record_url` by name and records the refusal rather than
    stopping the whole plan — one malformed row must not hide every other page's
    plan (rule 7: nothing is lost silently, refusals are recorded, not escalated
    into a blanket failure). A row whose identifier collides with an existing
    page under a *different* `source` is a data inconsistency this parser cannot
    silently resolve, so that one does stop the build.
    """
    pages: dict[str, dict[str, Any]] = {}
    refusals: list[dict[str, Any]] = []

    for row in rows:
        record_id = row.get("record_id")
        record_url = row.get("record_url")
        split = row.get("split")
        source = row.get("source")
        try:
            parsed = parse_record_url(record_url)
        except CorpusRefusal as error:
            reason = str(error).split(":", 1)[0]
            refusals.append(
                {
                    "record_id": record_id,
                    "record_url": record_url,
                    "reason": reason,
                    "detail": str(error),
                }
            )
            continue

        volume, designation = _volume_and_designation(parsed.identifier)
        page = pages.get(parsed.identifier)
        if page is None:
            page_physical_id = physical_page_id(CORPUS_ID, f"{source}/{volume}", designation)
            page = {
                "identifier": parsed.identifier,
                "info_url": f"https://{parsed.host}/iiif/2/{parsed.identifier_encoded}/info.json",
                "image_url_candidates": _image_urls(parsed.host, parsed.identifier_encoded),
                "source": source,
                "volume": volume,
                "designation": designation,
                "physical_page_id": page_physical_id,
                "splits_present": set(),
                "records": [],
            }
            pages[parsed.identifier] = page
        elif page["source"] != source:
            raise CorpusRefusal(
                f"inconsistent-source-for-identifier: {parsed.identifier!r} carries "
                f"source {source!r} on record {record_id!r} but {page['source']!r} "
                "on an earlier record for the same page"
            )

        page["splits_present"].add(split)
        page["records"].append(
            {
                "record_id": record_id,
                "physical_act_id": physical_act_id(page["physical_page_id"], record_id),
                "region": parsed.region,
                "split": split,
            }
        )

    page_list = []
    for identifier in sorted(pages):
        page = pages[identifier]
        page["splits_present"] = sorted(page["splits_present"])
        page["records"] = sorted(page["records"], key=lambda record: record["record_id"])
        page_list.append(page)

    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source_row_snapshot_self_hash": source_row_snapshot_self_hash,
        "pages": page_list,
        "refusals": sorted(refusals, key=lambda entry: (entry["reason"], entry["record_id"] or "")),
        "measurements": _measure(page_list, refusals),
    }
    body["self_hash"] = self_hash(body)
    return validate_plan(body)


_PAGE_FIELDS = frozenset(
    {
        "identifier",
        "info_url",
        "image_url_candidates",
        "source",
        "volume",
        "designation",
        "physical_page_id",
        "splits_present",
        "records",
    }
)
_RECORD_FIELDS = frozenset({"record_id", "physical_act_id", "region", "split"})
_REFUSAL_FIELDS = frozenset({"record_id", "record_url", "reason", "detail"})
_TOP_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "source_row_snapshot_self_hash",
        "pages",
        "refusals",
        "measurements",
        "self_hash",
    }
)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def validate_plan(plan: Any) -> dict[str, Any]:
    """Refuse a plan that is not exactly `recordgold-fetch-plan.v1`, closed and self-consistent."""
    plan = _closed(plan, _TOP_FIELDS, "fetch plan")
    if plan["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {plan['schema']!r}")
    if plan["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {plan['corpus_id']!r}")

    for page in plan["pages"]:
        page = _closed(page, _PAGE_FIELDS, f"page {page.get('identifier')!r}")
        if not isinstance(page["splits_present"], list) or page["splits_present"] != sorted(
            set(page["splits_present"])
        ):
            raise CorpusRefusal(
                f"malformed-record: page {page['identifier']!r} splits_present must be sorted and unique"
            )
        for split in page["splits_present"]:
            if split not in SPLITS:
                raise CorpusRefusal(
                    f"malformed-record: page {page['identifier']!r} names unknown split {split!r}"
                )
        if not page["records"]:
            raise CorpusRefusal(f"malformed-record: page {page['identifier']!r} carries no records")
        for record in page["records"]:
            _closed(record, _RECORD_FIELDS, f"record on page {page['identifier']!r}")

    for refusal in plan["refusals"]:
        _closed(refusal, _REFUSAL_FIELDS, "refusal entry")

    if not verify_self_hash(plan):
        raise CorpusRefusal(
            "self-hash-mismatch: fetch plan self_hash does not verify against its own content"
        )
    return plan
