"""The door: what may enter at all, decided by bytes alone.

The door owns no directory. It writes its admissions and refusals into the
Exemplar's, so the record of what arrived and the record of what was sealed sit
together — a refusal filed somewhere nothing downstream reads is a refusal that has
been lost, which GOVERNANCE 2 does not allow.

The one decoder-routing module (`admission.py`) decides from source bytes, never a
declared extension. Its configuration names how a source is read, not formats to
decline: ordinary rasters are decoder-backed; PDF and TIFF page containers fan out
and render once. PDFium paints the complete visible PDF page rather than extracting
an image XObject, so text beside an image stays in the sealed pixels.

Two invariants from the harvest still shape this. **#1: only images enter, verified
by decoding, not by extension** — now the real structural decode, not a magic-byte
check. **#3: a refused file is never silently omitted** — every refusal is an
artifact with a reason drawn from `admission.RefusalReason`'s closed set, and an
input set that admitted nothing is a loud failure rather than a green run with no
output.

**Two ways in, and the difference between them is not a flag.** The fixture path
runs the walking skeleton on the repository's own declared synthetic pages, and it
refuses to treat any other folder as a fixture — fixture status comes from the
declared fixture root and the `load_fixture` manifest, never from a caller's word
(ruling 2026-08-04, item 1). Everything else is real input: it is gated before a
byte is read, and the approval that admitted it is sealed into `run.json`'s own
self-hashed authority as the run's `ingress`, so a later reader asks the run
authority rather than an optional field on a stage artifact that could simply be
absent.

**No real image has been touched.** The real path exists, is gated, and is proven
against synthetic bytes standing in for real input; nothing has been pointed at a
real register page, and nothing may be until Tyrel approves the data-handling gate
package.

Invoked as a program:

    python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id>
    python pipeline/1_exemplar/door.py --run-root <dir> --run-id <id> \
        --submission-folder <dir> --submission-manifest <path> --approval-record <path>
"""

import json
import sys
from pathlib import Path
from typing import Any, Callable, Final, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
# The one folder in this repository whose contents are declared synthetic. A
# caller-named folder is real input, whatever it is called and whatever it holds.
DECLARED_SYNTHETIC_FIXTURE_ROOT: Final = ROOT / "proof"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import admission  # noqa: E402
import pdf_render  # noqa: E402
import render_config  # noqa: E402
from admission import RefusalReason  # noqa: E402
from image_formats import (  # noqa: E402
    MAX_DIMENSION,
    MAX_PIXELS,
    MAX_SOURCE_BYTES,
    MAX_TIFF_PAGES,
    FormatRefusal,
    count_raster_pages,
    raster_renderer_recipe,
    render_raster_page,
    sniff,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    APPROVAL_GATED_REAL_INGRESS,
    ApprovalRecordReference,
    approval_gated_real_ingress_record,
    parse_data_gate_ingress_record,
    synthetic_fixture_ingress_record,
)
from common.contracts.canonical import digest_bytes, digest_of, self_hash  # noqa: E402
from common.contracts.errors import ApprovalRefusal, ContractError  # noqa: E402
from common.contracts.stages import DOOR  # noqa: E402
from common.recovery import load_recovery_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    load_fixture,
    run_config_bindings,
    run_stage,
    scenario_for,
    stage_parser,
)
from operations.submit import gate, inventory  # noqa: E402
from operations.submit import submit as submission_ledger  # noqa: E402


class SourceEntry(NamedTuple):
    """One standalone raster or one page fanned out from a source container."""

    ordinal: int
    declared_path: str
    declared_sha256: str | None
    container_page_index: int | None = None
    declared_size: int | None = None
    ledger_sha256: str | None = None


class _Decision(NamedTuple):
    outcome: str
    reason: str | None
    digest: str | None
    store_bytes: bytes | None
    geometry: tuple[int, int] | None
    rendered_from: dict[str, Any] | None = None


DOOR_REFUSAL_REPORT_SCHEMA: Final = "door-refusal-report.v0"
DOOR_REFUSAL_REPORT_SUBJECT: Final = "refusal-report"


def declared_digests(fixture: dict, scenario: str) -> dict[int, str]:
    """The digest each page is declared to have, per ordinal, for this scenario.

    A `page_refusal` row substitutes a declared digest the checked-in bytes cannot
    match, so the refusal scenarios exercise the door's real inspection path — the
    same comparison, the same refusal artifact — rather than any scenario-aware
    branch that a real door would not have.
    """
    declared = {page["ordinal"]: page["sha256"] for page in fixture["page"]}
    for row in fixture.get("page_refusal", []):
        if row["scenario"] != scenario:
            continue
        if row["ordinal"] not in declared:
            raise ContractError(
                f"page_refusal names ordinal {row['ordinal']}, which no declared page has"
            )
        declared[row["ordinal"]] = row["declared_sha256"]
    return declared


def decide(
    data: bytes,
    source: SourceEntry,
    policy: dict[str, str],
    pdf_settings: render_config.PdfRenderSettings | None = None,
) -> _Decision:
    """Decide one raster or one source-container page by its actual bytes."""
    if pdf_settings is None:
        pdf_settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    verdict = admission.classify_detected_format(sniff(data), policy)
    whole_digest = digest_bytes(data)
    if source.declared_sha256 is not None and whole_digest != source.declared_sha256:
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.DIGEST_MISMATCH,
                f"computed {whole_digest}, but {source.declared_sha256} was declared",
            ),
            whole_digest,
            None,
            None,
        )
    if source.container_page_index is None:
        if verdict == admission.RENDER_PAGES and 0 < len(data) <= MAX_SOURCE_BYTES:
            return _Decision(
                "refused",
                admission.reason(
                    RefusalReason.UNSUPPORTED_VARIANT,
                    "a page container must be declared with a page index; this one carries none",
                ),
                whole_digest,
                None,
                None,
            )
        result = admission.inspect_source(
            data, declared_sha256=source.declared_sha256, policy=policy
        )
        return _Decision(
            result.outcome,
            result.reason,
            result.digest,
            data if result.outcome == "admitted" else None,
            result.geometry,
        )

    detected = sniff(data)
    try:
        if detected == "pdf":
            opened = pdf_render.open_document(data)
            try:
                rendered = pdf_render.render_page(opened, source.container_page_index, pdf_settings)
            finally:
                pdf_render.close_document(opened)
            page_bytes = rendered.png_bytes
            rendered_from = {
                "container_format": detected,
                "container_sha256": whole_digest,
                "container_page_index": source.container_page_index,
                "render_contract": rendered.contract,
            }
        else:
            page_count = count_raster_pages(data)
            if page_count == 1 and verdict != admission.RENDER_PAGES:
                return _Decision(
                    "refused",
                    admission.reason(
                        RefusalReason.UNSUPPORTED_VARIANT,
                        f"{detected or 'unknown'} was declared with a page index but decoder "
                        "reports one frame",
                    ),
                    whole_digest,
                    None,
                    None,
                )
            page_bytes, _geometry, contract = render_raster_page(data, source.container_page_index)
            rendered_from = {
                "container_format": detected,
                "container_sha256": whole_digest,
                "container_page_index": source.container_page_index,
                "render_contract": contract,
            }
    except pdf_render.PdfRefusal as error:
        return _Decision("refused", str(error), None, None, None)
    except FormatRefusal as error:
        return _Decision(
            "refused",
            admission.reason(admission._refusal_code(error), str(error)),
            None,
            None,
            None,
        )

    checked = admission.inspect_source(page_bytes, declared_sha256=None, policy=policy)
    if checked.outcome != "admitted":
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.CORRUPT,
                f"the rendered page did not itself admit: {checked.reason}",
            ),
            None,
            None,
            None,
        )
    return _Decision("admitted", None, checked.digest, page_bytes, checked.geometry, rendered_from)


def expand_sources(
    files: list[dict[str, Any]], read_bytes: Callable[[str], bytes], policy: dict[str, str]
) -> list[SourceEntry]:
    """Expand configured PDF/TIFF containers to stable page ordinals.

    Counting inspects only enough to learn a page count; it does not produce page
    pixels.  Any source that cannot be read or counted still receives one ordinal,
    so the later decision can publish a named alarm rather than lose it.
    """
    ordinal = 0
    sources: list[SourceEntry] = []
    for row in sorted(files, key=lambda item: item["relative_path"]):
        path, declared_sha256 = row["relative_path"], row["sha256"]
        declared_size = row.get("bytes")
        ledger_sha256 = row.get("ledger_sha256")
        if declared_size is not None and (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size < 0
        ):
            # No `path` in the message: `run_stage` prints every ContractError to
            # stderr, and a declared path is what the data-handling policy's logging
            # rule keeps out of exactly that channel.
            raise ContractError(
                "a submitted source declares no non-negative byte count; the source "
                "manifest names it by ordinal"
            )
        try:
            data = read_bytes(path)
        except OSError:
            ordinal += 1
            sources.append(
                SourceEntry(ordinal, path, declared_sha256, None, declared_size, ledger_sha256)
            )
            continue
        detected = sniff(data)
        route = admission.classify_detected_format(detected, policy)
        if len(data) > MAX_SOURCE_BYTES:
            ordinal += 1
            sources.append(
                SourceEntry(ordinal, path, declared_sha256, None, declared_size, ledger_sha256)
            )
            continue
        try:
            page_count = (
                pdf_render.count_pages(data) if detected == "pdf" else count_raster_pages(data)
            )
        except (pdf_render.PdfRefusal, FormatRefusal):
            if route != admission.RENDER_PAGES:
                ordinal += 1
                sources.append(
                    SourceEntry(ordinal, path, declared_sha256, None, declared_size, ledger_sha256)
                )
                continue
            ordinal += 1
            sources.append(
                SourceEntry(ordinal, path, declared_sha256, 0, declared_size, ledger_sha256)
            )
            continue
        # PDF/TIFF are declared page containers even when there is one page. For
        # every other decoder-backed image, a reported multi-frame source is also
        # fanned out. Retaining an animation as one raster would silently drop all
        # but frame zero downstream; one-frame rasters retain their original bytes.
        if route != admission.RENDER_PAGES and page_count == 1:
            ordinal += 1
            sources.append(
                SourceEntry(ordinal, path, declared_sha256, None, declared_size, ledger_sha256)
            )
            continue
        for page_index in range(page_count):
            ordinal += 1
            sources.append(
                SourceEntry(
                    ordinal, path, declared_sha256, page_index, declared_size, ledger_sha256
                )
            )
    return sources


def process_sources(
    context: StageContext,
    tree: RunTree,
    sources: list[SourceEntry],
    read_bytes: Callable[[str], bytes],
    *,
    policy: dict[str, str],
    pdf_settings: render_config.PdfRenderSettings | None = None,
    data_policy: dict[str, Any] | None = None,
) -> int:
    """Admit or refuse every declared source. Returns the count admitted.

    Fixture status is read from the self-hashed run authority, never accepted from
    a caller argument. The real-input gate is checked first, before a single file
    is opened, through the approval reference that same authority carries.

    `read_bytes` is called once per distinct `declared_path` within *this* call,
    even when several ordinals (a PDF's pages) share one: a source is read once and
    every one of its pages is rendered from that single copy in memory.

    Per-file, never per-folder (harvest #2): one unreadable or refused source does
    not stop the rest from being decided. Duplicate files are refused by their
    source bytes and declared path. Byte-identical pages within one PDF remain
    distinct pages; a second path carrying the same PDF bytes is a duplicate file.
    """
    mode, _ingress_hash, approval_reference = parse_data_gate_ingress_record(
        context.run.get("ingress")
    )
    if pdf_settings is None:
        pdf_settings = render_config.load_pdf_render_settings(minimum_dpi=pdf_render.MIN_RENDER_DPI)
    if mode == APPROVAL_GATED_REAL_INGRESS:
        if data_policy is None:
            gate.enforce(approval=None, policy=None)
        approval = gate.load_approval(approval_reference, root=tree.root, policy=data_policy)
        gate.enforce(approval=approval, policy=data_policy)
    admitted = 0
    seen_sources: dict[str, tuple[str, int]] = {}
    cache: dict[str, bytes] = {}
    for source in sorted(sources, key=lambda item: item.ordinal):
        if source.declared_size is not None and source.declared_size > MAX_SOURCE_BYTES:
            # Refused for its own size, never as a duplicate of something else. Two
            # oversized copies of one file are two oversized files, and each is told
            # the truth about itself.
            _publish(
                context,
                source,
                outcome="refused",
                reason=admission.reason(
                    RefusalReason.TOO_LARGE,
                    f"{source.declared_size} bytes exceeds the "
                    f"{MAX_SOURCE_BYTES}-byte admission limit",
                ),
                approval_reference=approval_reference,
            )
            continue
        try:
            data = cache.get(source.declared_path)
            if data is None:
                data = read_bytes(source.declared_path)
                cache[source.declared_path] = data
        except OSError as error:
            _publish(
                context,
                source,
                outcome="refused",
                reason=admission.reason(RefusalReason.UNREADABLE, str(error)),
                approval_reference=approval_reference,
            )
            continue

        if source.declared_size is not None and len(data) != source.declared_size:
            _publish(
                context,
                source,
                outcome="refused",
                reason=admission.reason(
                    RefusalReason.DIGEST_MISMATCH,
                    f"the source now has {len(data)} bytes, but {source.declared_size} bytes "
                    "were recorded in its filename ledger",
                ),
                approval_reference=approval_reference,
            )
            continue

        actual_digest = digest_bytes(data)
        if source.declared_sha256 is not None and actual_digest != source.declared_sha256:
            _publish(
                context,
                source,
                outcome="refused",
                reason=admission.reason(
                    RefusalReason.DIGEST_MISMATCH,
                    f"computed {actual_digest}, but {source.declared_sha256} was declared",
                ),
                approval_reference=approval_reference,
            )
            continue
        decision = decide(data, source, policy, pdf_settings)

        if decision.outcome == "refused":
            _publish(
                context,
                source,
                outcome="refused",
                reason=decision.reason,
                approval_reference=approval_reference,
            )
            continue

        first = seen_sources.get(actual_digest)
        if first is not None and first[0] != source.declared_path:
            _publish(
                context,
                source,
                outcome="refused",
                reason=admission.duplicate_reason(first[1]),
                approval_reference=approval_reference,
            )
            continue

        # Registered *after* the decision, and only on an admission: the duplicate
        # reason says "already admitted as source-N", and a record may only claim
        # what actually happened (GOVERNANCE 10). Registering before `decide` made
        # a refused source the "first", so a second copy of two corrupt files was
        # told its twin had been admitted, and the census read "one corrupt file,
        # one duplicate" when the truth was two corrupt files.
        #
        # **The order of the two checks above carries the same weight, and this
        # comment used to give all the credit to the line below.** Refusing on the
        # source's own merits *before* consulting `seen_sources` is what makes the
        # registration point survivable either way; move the duplicate check above
        # the refusal check and a second broken file is told it duplicates the
        # first no matter when the digest was registered. Both were confirmed by
        # breaking each in turn — only the reordering changed a test's outcome, so
        # the ordering is the load-bearing half and it is written down here now.
        seen_sources.setdefault(actual_digest, (source.declared_path, source.ordinal))
        _, published = tree.put_blob(DOOR, decision.store_bytes)
        extra: dict[str, Any] = {
            "sha256": decision.digest,
            "stored_at": published.relative_path,
            "geometry": {"width": decision.geometry[0], "height": decision.geometry[1]},
        }
        if decision.rendered_from is not None:
            extra["rendered_from"] = decision.rendered_from
        _publish(
            context,
            source,
            outcome="admitted",
            payload_extra=extra,
            inputs=[context.input_ref(published.relative_path)],
            approval_reference=approval_reference,
        )
        admitted += 1

    return admitted


def _publish(
    context: StageContext,
    source: SourceEntry,
    *,
    outcome: str,
    reason: str | None = None,
    payload_extra: dict | None = None,
    inputs: list[dict[str, str]] | None = None,
    approval_reference: ApprovalRecordReference | None = None,
) -> None:
    payload: dict = {
        "declared_path": source.declared_path,
        "declared_sha256": source.declared_sha256,
        "ordinal": source.ordinal,
    }
    if source.declared_size is not None:
        payload["declared_bytes"] = source.declared_size
    if source.ledger_sha256 is not None:
        payload["ledger_sha256"] = source.ledger_sha256
    if outcome == "refused":
        payload["reason"] = reason
    else:
        payload.update(payload_extra or {})
    if approval_reference is not None:
        payload["data_gate_approval_ref"] = approval_reference.to_record()
    context.publish(
        kind="admission",
        subject_id=f"source-{source.ordinal}",
        outcome=outcome,
        inputs=inputs or [],
        payload=payload,
    )


def publish_refusal_report(context: StageContext) -> str | None:
    """Seal every door alarm into one private, filename-bearing report.

    The per-source admission artifacts remain the authority.  This report is their
    self-hashed, input-referenced index for an operator who needs a named list
    without putting filenames or image bytes into terminal output.  It is an
    ordinary run-tree artifact, not a sixth on-disk file shape.
    """
    rows: list[dict[str, Any]] = []
    inputs: list[dict[str, str]] = []
    for entry in context.tree.build_manifest(DOOR)["artifacts"]:
        if entry["kind"] != "admission" or entry["outcome"] != "refused":
            continue
        record = context.tree.read_artifact(DOOR, "admission", entry["artifact_id"])
        payload = record["payload"]
        ordinal, path, refusal = (
            payload.get("ordinal"),
            payload.get("declared_path"),
            payload.get("reason"),
        )
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("a refused door admission has no integer source ordinal")
        if not isinstance(path, str) or not path:
            raise ContractError("a refused door admission has no declared filename")
        # Reading the closed code back is what prevents a free-text report from
        # turning a producer bug into the operator's only explanation.
        admission.reason_code(refusal)
        rows.append({"ordinal": ordinal, "declared_path": path, "reason": refusal})
        inputs.append({"relative_path": entry["relative_path"], "sha256": entry["sha256"]})
    if not rows:
        return None
    payload: dict[str, Any] = {
        "schema": DOOR_REFUSAL_REPORT_SCHEMA,
        "refusals": sorted(rows, key=lambda row: row["ordinal"]),
    }
    payload["self_hash"] = self_hash(payload)
    published = context.publish(
        kind="refusal-report",
        subject_id=DOOR_REFUSAL_REPORT_SUBJECT,
        outcome="refused",
        inputs=inputs,
        payload=payload,
    )
    return published.relative_path


def require_some_admitted(admitted: int, tree: RunTree, refusal_report: str | None) -> None:
    """An empty or wholly refused input set is a loud failure (harvest #3).

    The terminal carries the count and private report location, while the report
    itself names every source and reason. This preserves filenames as citation links
    without placing them in a captured terminal stream.
    """
    if admitted != 0:
        return
    total, census = _refusal_census(tree)
    named = ", ".join(f"{code}: {count}" for code, count in sorted(census.items()))
    raise ContractError(
        f"the door admitted nothing: {total} source(s) submitted, "
        f"{sum(census.values())} refused ({named or 'no refusal was recorded either'}). "
        f"Private named refusal report: {refusal_report or 'unavailable'}. "
        "An empty or wholly unreadable input set is a loud failure, never a green run with no "
        "output (harvest #3)"
    )


def _refusal_census(tree: RunTree) -> tuple[int, dict[str, int]]:
    """Count the published refusals by closed-set reason code.

    **Nothing in here may raise.** It runs only on the failure path, to describe a
    failure that has already happened, and an exception from reading a damaged
    artifact would replace "the door admitted nothing" with something about JSON —
    the primary failure masked by a secondary one, which is a worse answer to
    GOVERNANCE 2 than a partial census. So an artifact that cannot be read or whose
    reason is outside the closed set is counted under a name that says so, and the
    loud failure still says what it is.
    """
    census: dict[str, int] = {}
    total = 0
    try:
        entries = tree.build_manifest(DOOR)["artifacts"]
    except (OSError, ValueError, ContractError):
        return 0, {"the door's own census could not be read": 1}
    for entry in entries:
        if entry.get("kind") != "admission":
            continue
        total += 1
        try:
            record = json.loads(tree.read_bytes(entry["relative_path"]).decode("utf-8"))
            if record["outcome"] != "refused":
                continue
            code = admission.reason_code(record["payload"].get("reason")).value
        except (OSError, ValueError, KeyError, ContractError):
            code = "unreadable record"
        census[code] = census.get(code, 0) + 1
    return total, census


def declared_synthetic_fixture_root(requested_root: str) -> Path:
    """The one root in this repository whose contents are declared synthetic.

    Ruling 2026-08-04, item 1: fixture status comes from the declared fixture
    manifest, never from a caller flag, a filename suffix, or a folder name. A
    caller pointing `--fixture-root` at its own directory is pointing at real
    input, and this is what says so instead of believing it.
    """
    try:
        candidate = Path(requested_root).resolve(strict=True)
    except OSError as error:
        raise ContractError(
            f"the declared synthetic fixture root {requested_root!r} could not be resolved"
        ) from error
    if candidate != DECLARED_SYNTHETIC_FIXTURE_ROOT.resolve():
        raise ContractError(
            f"{requested_root!r} is not the declared synthetic fixture root "
            f"({DECLARED_SYNTHETIC_FIXTURE_ROOT}); a caller-owned folder is real input "
            "and goes through --submission-folder, where the data-handling gate is"
        )
    return candidate


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Create the run with an explicitly supplied chair implementation.

    The command-line default is the production registry. Tests supply an
    independent deterministic implementation through this seam; no command-line
    option chooses among implementations, chairs, revisions, recipes, or caches.
    """
    parser = stage_parser(__doc__.splitlines()[0])
    parser.add_argument(
        "--submission-folder",
        help="a real local submission; any input arriving this way needs a current approval",
    )
    parser.add_argument(
        "--approval-record",
        help="path to Tyrel's sealed data-gate approval record for the current policy",
    )
    parser.add_argument(
        "--submission-manifest",
        help=(
            "self-hashed local filename ledger made before transfer; required with a real "
            "submission folder"
        ),
    )
    parser.add_argument(
        "--data-gate-policy",
        default=str(gate.DEFAULT_POLICY_PATH),
        help="the data-handling policy whose canonical hash the approval must name",
    )
    args = parser.parse_args()
    registry = registry_factory(args.models_config)

    if args.submission_folder is not None:
        return real_submission(args, registry)
    if args.approval_record is not None or args.submission_manifest is not None:
        raise ContractError(
            "an approval record or submission filename ledger is meaningful only with a real "
            "submission folder; the walking skeleton's declared synthetic pages are not "
            "gated input"
        )
    return fixture_submission(args, registry)


def fixture_submission(args, registry) -> int:
    """The walking skeleton: declared synthetic pages, no gate, sealed as such."""
    fixture_root = declared_synthetic_fixture_root(args.fixture_root)
    fixture = load_fixture(str(fixture_root))
    scenario_for(fixture, args.scenario)
    declared = declared_digests(fixture, args.scenario)
    policy = admission.load_format_policy()
    pdf_settings = render_config.load_pdf_render_settings(
        Path(args.pdf_render_config),
        target_override=args.pdf_target_dpi,
        minimum_dpi=pdf_render.MIN_RENDER_DPI,
    )
    bindings = run_config_bindings(
        registry.config,
        fixture,
        args.scenario,
        pdf_render_config_path=args.pdf_render_config,
        pdf_target_dpi=args.pdf_target_dpi,
        recovery_config_path=args.recovery_config,
    )

    # The door creates the run: it is the first thing that knows what arrived, so
    # it is the only stage that can bind a run id to its inputs. The manifest
    # carries the *declared* digests — what this run believed about its sources —
    # so a refusal and the declaration it was refused against tell one story.
    tree = RunTree.create(
        Path(args.run_root),
        args.run_id,
        source_manifest=[
            {
                "relative_path": page["path"],
                "sha256": declared[page["ordinal"]],
                "ordinal": page["ordinal"],
            }
            for page in fixture["page"]
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
        render_settings={"pdf": pdf_settings.to_record()},
    )
    context = _door_context(tree, fixture, args.scenario, args, registry)
    sources = [
        SourceEntry(page["ordinal"], page["path"], declared[page["ordinal"]])
        for page in fixture["page"]
    ]
    admitted = process_sources(
        context,
        tree,
        sources,
        lambda declared_path: (fixture_root / declared_path).read_bytes(),
        policy=policy,
        pdf_settings=pdf_settings,
    )
    refusal_report = publish_refusal_report(context)
    context.finish(DOOR)
    _announce_refusal_report(tree, refusal_report)
    require_some_admitted(admitted, tree, refusal_report)
    return EXIT_COMPLETE


def real_submission(args, registry) -> int:
    """Gate a local folder, then admit its bytes into a run that says it was gated.

    Order matters and is the whole point: the approval is verified, then the
    storage roots, then the folder is inventoried, then the run is created with the
    approval sealed into its authority, and only then is a byte published. A
    missing, stale or damaged approval means nothing was read and nothing exists.
    """
    data_policy = gate.load_policy(Path(args.data_gate_policy))
    if args.approval_record is None:
        gate.enforce(approval=None, policy=data_policy)
    if args.submission_manifest is None:
        raise ContractError(
            "a real submission requires --submission-manifest: the self-hashed filename "
            "ledger is how its copied bytes are matched back to the original set"
        )
    approval, reference = gate.read_external_approval(Path(args.approval_record), data_policy)

    roots = gate.approved_storage_roots(data_policy)
    # The *resolved* paths are used from here on, as `submit.py` does and for the
    # same reason: checking one path and then opening another is where a
    # check-then-use race lives, and the resolved values are already in hand.
    submission_folder = gate.require_approved_storage_location(
        Path(args.submission_folder), roots, "submitted folder"
    )
    run_root = gate.require_approved_storage_location(Path(args.run_root), roots, "run root")
    manifest_path = gate.require_approved_storage_location(
        Path(args.submission_manifest), roots, "submission filename ledger"
    )
    for location, label in (
        (run_root, "run root"),
        (manifest_path, "submission filename ledger"),
    ):
        if location.is_relative_to(submission_folder):
            raise ContractError(
                f"the {label} cannot live inside the submitted folder; otherwise the next "
                "inventory includes pipeline-produced records as submitted sources"
            )
    ledger = submission_ledger.load_manifest(manifest_path)
    if ledger["authorized_by"] != reference.to_record():
        raise ApprovalRefusal(
            "the submission filename ledger names a different data-gate approval than the "
            "approval record supplied to the door"
        )

    format_policy = admission.load_format_policy()
    pdf_settings = render_config.load_pdf_render_settings(
        Path(args.pdf_render_config),
        target_override=args.pdf_target_dpi,
        minimum_dpi=pdf_render.MIN_RENDER_DPI,
    )
    found = inventory.read_submission(submission_folder, max_bytes=MAX_SOURCE_BYTES)
    found_paths = {source.relative_path for source in found}
    declared_paths = {row["relative_path"] for row in ledger["files"]}
    unexpected = found_paths - declared_paths
    if unexpected:
        raise ContractError(
            "the submitted folder contains file(s) absent from its self-hashed filename "
            f"ledger ({len(unexpected)} extra); no run was created over an ambiguous set"
        )
    bytes_by_path = {source.relative_path: source.data for source in found}

    def read_bytes(relative_path: str) -> bytes:
        try:
            data = bytes_by_path[relative_path]
        except KeyError as error:
            raise OSError(
                "a source named by the filename ledger is absent after transfer"
            ) from error
        if data is None:
            # The inventory kept an exact digest and size but not the bytes. The
            # declared-size branch refuses it before this reader is called; this is
            # the fail-closed fallback if those two records ever drift.
            raise OSError(f"source exceeds the {MAX_SOURCE_BYTES}-byte admission limit")
        return data

    sources = expand_sources(
        [
            {
                "relative_path": source["relative_path"],
                "sha256": source["sha256"],
                "bytes": source["bytes"],
                "ledger_sha256": ledger["self_hash"],
            }
            for source in ledger["files"]
        ],
        read_bytes,
        format_policy,
    )
    bindings = _real_bindings(
        registry.config,
        ledger,
        data_policy,
        reference,
        format_policy,
        pdf_settings,
        load_recovery_policy(args.recovery_config),
    )
    tree = RunTree.create(
        run_root,
        args.run_id,
        source_manifest=[
            {
                "relative_path": source.declared_path,
                "sha256": source.declared_sha256,
                "ordinal": source.ordinal,
                "bytes": source.declared_size,
                "ledger_sha256": source.ledger_sha256,
                "container_page_index": source.container_page_index,
            }
            for source in sources
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=approval_gated_real_ingress_record(gate.policy_hash(data_policy), reference),
        render_settings={"pdf": pdf_settings.to_record()},
    )
    stored, _ = tree.write_approval_record(approval)
    if stored.to_record() != reference.to_record():
        raise ApprovalRefusal(
            "the data-gate approval changed between verification and storage; the run "
            "authority names one record and the tree holds another"
        )

    context = _door_context(tree, {}, "real-submission", args, registry)
    admitted = process_sources(
        context,
        tree,
        sources,
        read_bytes,
        policy=format_policy,
        pdf_settings=pdf_settings,
        data_policy=data_policy,
    )
    refusal_report = publish_refusal_report(context)
    context.finish(DOOR)
    _announce_refusal_report(tree, refusal_report)
    require_some_admitted(admitted, tree, refusal_report)
    return EXIT_COMPLETE


def _announce_refusal_report(tree: RunTree, refusal_report: str | None) -> None:
    """Give the terminal only a count and private report location, never a name."""
    if refusal_report is None:
        return
    _total, census = _refusal_census(tree)
    print(
        f"{sum(census.values())} door refusal(s); private refusal report: {refusal_report}",
        file=sys.stderr,
    )


def _real_bindings(
    models, ledger, data_policy, reference, format_policy, pdf_settings, recovery_policy
) -> dict[str, Any]:
    """The sealed configuration facts for a real submission.

    The source manifest binds the bytes. The configuration digest binds everything
    else that shaped what the door did: the model roster, the data-handling policy
    version, the exact approval that admitted the corpus, decoder routing, and the
    versions/settings that render pages. A run resumed under different versions or
    routing is a different run wearing an old name, and `RunTree.create` refuses
    it before anything is written.
    """
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "submission": [
                    {
                        "relative_path": source["relative_path"],
                        "sha256": source["sha256"],
                        "bytes": source["bytes"],
                    }
                    for source in ledger["files"]
                ],
                "submission_ledger_sha256": ledger["self_hash"],
                "data_gate_policy_hash": gate.policy_hash(data_policy),
                "data_gate_approval_ref": reference.to_record(),
                "format_policy": format_policy,
                "door_execution_recipe": _door_execution_recipe(pdf_settings),
                "recovery_policy": recovery_policy,
                "models": models.to_record(),
            }
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
    }


def _door_execution_recipe(pdf_settings) -> dict[str, Any]:
    """Facts that change page admission or pixels, sealed before real writes."""
    return {
        "pdf": pdf_render.renderer_recipe(pdf_settings),
        "raster": raster_renderer_recipe(),
        "limits": {
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_dimension": MAX_DIMENSION,
            "max_pixels": MAX_PIXELS,
            "max_tiff_pages": MAX_TIFF_PAGES,
            "max_pdf_pages": pdf_render.MAX_PAGES,
        },
    }


def _door_context(tree: RunTree, fixture: dict, scenario: str, args, registry) -> StageContext:
    run = tree.read_run()
    return StageContext(
        tree=tree,
        run=run,
        fixture=fixture,
        scenario=scenario,
        stage=DOOR,
        adapter_revision=adapter_recipe_for(run, DOOR),
        args=args,
        registry=registry,
    )


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
