"""The door: what may enter at all, decided by bytes alone.

The door owns no directory. It writes its admissions and refusals into the
Exemplar's, so the record of what arrived and the record of what was sealed sit
together — a refusal filed somewhere nothing downstream reads is a refusal that has
been lost, which GOVERNANCE 2 does not allow.

**Spec 03 replaces the walking skeleton's toy PNG-signature check with the real
thing.** Admission is decided by `admission.py` — the one format policy — against
the structural validators in `image_formats.py`, never by a file's declared
extension.

**The admission list decides for every format, including the multi-page ones.**
A `render-pages` source is fanned out into its pages and rendered once, here,
through the door-private `pdf_render.py`; no module outside this stage imports it,
which is what makes "no later stage may re-render" true by construction (spec 03,
test 4) rather than by convention. But *whether* a format is fanned out at all is
`admission.classify_detected_format`'s answer, asked once before the pages are
counted and again before one is rendered. This module names no format anywhere: a
`sniff(data) == "pdf"` branch here would be the admission rule existing twice —
the exact drift spec 03 was written to kill — and under the shipped list, which
refuses PDF, it would have fanned out and sealed pages the configuration says are
not admitted.

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
        --submission-folder <dir> --approval-record <path>
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
from admission import RefusalReason  # noqa: E402
from image_formats import MAX_SOURCE_BYTES, sniff  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import (  # noqa: E402
    APPROVAL_GATED_REAL_INGRESS,
    ApprovalRecordReference,
    approval_gated_real_ingress_record,
    parse_data_gate_ingress_record,
    synthetic_fixture_ingress_record,
)
from common.contracts.canonical import digest_bytes, digest_of  # noqa: E402
from common.contracts.errors import ApprovalRefusal, ContractError  # noqa: E402
from common.contracts.stages import DOOR, writing_directory  # noqa: E402
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


class SourceEntry(NamedTuple):
    """One declared page: a standalone raster file, or one page out of a PDF.

    `pdf_page_index` is `None` for a standalone file. Several ordinals may share
    one `declared_path` with different `pdf_page_index` values when that path names
    a multi-page PDF — the fan-out the door performs itself, because turning "N
    files" into "M pages" means opening each file to see what it is, which is the
    inspection admission is required to do by bytes rather than by name. Ordinals
    are always per-page, never per-file, so `RunTree.create`'s ordinal accounting
    needs no special case for a PDF at all.
    """

    ordinal: int
    declared_path: str
    declared_sha256: str | None
    pdf_page_index: int | None = None
    declared_size: int | None = None


class _Decision(NamedTuple):
    outcome: str
    reason: str | None
    digest: str | None
    store_bytes: bytes | None
    geometry: tuple[int, int] | None


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


def _prepared_document(data: bytes, declared_path: str, documents: dict[str, Any] | None) -> Any:
    """The parsed container for this source, parsed once however many pages it has.

    `render_page` used to take raw bytes and re-parse the whole cross-reference
    table and page tree per call, so an N-page source paid it N+1 times. The cache
    is per `process_sources` call and keyed by declared path, which is the same
    scope the byte cache beside it uses.
    """
    if documents is None:
        return pdf_render.open_document(data)
    prepared = documents.get(declared_path)
    if prepared is None:
        prepared = pdf_render.open_document(data)
        documents[declared_path] = prepared
    return prepared


def decide(
    data: bytes,
    source: SourceEntry,
    policy: dict[str, str],
    *,
    documents: dict[str, Any] | None = None,
) -> _Decision:
    """One source's admission decision. PDF pages and standalone rasters alike.

    **The policy decides first, for every source, whatever shape it has.** A page
    index on a `SourceEntry` is a claim about how the file was fanned out; it is
    never permission to skip the admission list. `expand_sources` asks the policy
    before it counts a page and this asks it again before it renders one, so a
    `render-pages` row turned to `refuse` refuses at both boundaries rather than at
    neither.

    `documents` is the caller's per-run cache of parsed containers, so a source is
    parsed once rather than once per page inside it.
    """
    verdict = admission.classify_detected_format(sniff(data), policy)
    if source.pdf_page_index is None:
        if verdict == admission.RENDER_PAGES:
            # A multi-page container reaching this branch means a manifest skipped
            # the fan-out `expand_sources` performs; refused rather than guessed at.
            return _Decision(
                "refused",
                admission.reason(
                    RefusalReason.UNSUPPORTED_VARIANT,
                    "a multi-page container must be declared with a page index; this "
                    "one carries none",
                ),
                digest_bytes(data),
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

    if verdict != admission.RENDER_PAGES:
        # The source was fanned out into pages under a policy that does not fan this
        # format out — a stale manifest, a hand-built entry, or a policy edited
        # between the fan-out and here. The admission list is the authority in every
        # one of those cases, and the refusal names what is actually true of *these*
        # bytes: a format the list refuses, bytes that match no signature, or — the
        # case that is neither — a format the list admits as a single image and was
        # nonetheless handed a page index. Reaching for "refuses by name" in that
        # last case would assert something the list does not say.
        detected = sniff(data)
        if verdict is RefusalReason.UNRECOGNIZED_FORMAT:
            code, detail = verdict, "bytes do not match any known image signature"
        elif verdict is RefusalReason.REFUSED_FORMAT:
            code, detail = verdict, admission.refused_format_detail(detected)
        else:
            code, detail = (
                RefusalReason.UNSUPPORTED_VARIANT,
                f"this source was declared with a page index, but the admission list "
                f"gives {detected} the action {verdict!r} rather than page rendering; "
                "a page index is a claim about a fan-out, never permission to skip the list",
            )
        return _Decision("refused", admission.reason(code, detail), digest_bytes(data), None, None)

    # A PDF-sourced page: the declared digest names the *whole file*, checked once
    # regardless of how many of its pages are being admitted, because a tampered
    # container is untrustworthy for every page inside it.
    whole_digest = digest_bytes(data)
    if source.declared_sha256 is not None and whole_digest != source.declared_sha256:
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.DIGEST_MISMATCH,
                f"computed {whole_digest} for the PDF, but {source.declared_sha256} was declared",
            ),
            whole_digest,
            None,
            None,
        )
    try:
        page_bytes, page_format = pdf_render.render_page(
            _prepared_document(data, source.declared_path, documents), source.pdf_page_index
        )
    except pdf_render.PdfRefusal as error:
        return _Decision("refused", str(error), None, None, None)

    # What was rendered is now inspected as though it had been submitted: the same
    # structural validators, the same one format policy. A renderer that emitted
    # something malformed must not get a pass for being ours.
    checked = admission.inspect_source(page_bytes, declared_sha256=None, policy=policy)
    if checked.outcome != "admitted":
        return _Decision(
            "refused",
            admission.reason(
                RefusalReason.CORRUPT,
                f"the rendered {page_format} page did not itself admit: {checked.reason}",
            ),
            None,
            None,
            None,
        )
    # The bytes stored are what was rendered, never the original PDF: "what is
    # sealed must be what was inspected" for a page means the page's own bytes.
    return _Decision("admitted", None, checked.digest, page_bytes, checked.geometry)


def expand_sources(
    files: list[dict[str, Any]], read_bytes: Callable[[str], bytes], policy: dict[str, str]
) -> list[SourceEntry]:
    """Turn a list of submitted files into a list of pages with ordinals assigned.

    `files` is a list of `{relative_path, sha256, bytes}` rows sorted by path, the
    shape `operations/submit/submit.py`'s sealed manifest writes. Synthetic callers
    may omit `bytes`; real inventory never does. A PDF's page count is read once
    through the same door-private `pdf_render` call that renders them; nothing here
    decodes a page early.

    **A source is fanned out only when the admission list says to fan that format
    out.** The decision is `admission.classify_detected_format`, the same one
    function the raster path reads — never a format name written into this module.
    A hardcoded `sniff(data) == "pdf"` here is the admission rule existing twice,
    which is precisely the defect spec 03 exists to kill; under it, a policy row
    reading `pdf = "refuse"` would be counted, fanned out and rendered anyway.

    Refusals are *not* decided here. One unreadable or unopenable source still
    occupies exactly one ordinal, and `process_sources` produces its named refusal
    artifact — so there is exactly one place in this module that decides an
    outcome, and the accounting cannot disagree with it.
    """
    ordinal = 0
    sources: list[SourceEntry] = []
    for row in sorted(files, key=lambda item: item["relative_path"]):
        path, declared_sha256 = row["relative_path"], row["sha256"]
        declared_size = row.get("bytes")
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
            sources.append(SourceEntry(ordinal, path, declared_sha256, None, declared_size))
            continue
        if (
            len(data) > MAX_SOURCE_BYTES
            or admission.classify_detected_format(sniff(data), policy) != admission.RENDER_PAGES
        ):
            ordinal += 1
            sources.append(SourceEntry(ordinal, path, declared_sha256, None, declared_size))
            continue
        try:
            page_count = pdf_render.count_pages(data)
        except pdf_render.PdfRefusal:
            # An unopenable PDF still occupies exactly one ordinal: it is one
            # declared source, refused as a whole, and the run-level source
            # manifest must still account for it by ordinal.
            ordinal += 1
            sources.append(SourceEntry(ordinal, path, declared_sha256, 0, declared_size))
            continue
        for page_index in range(page_count):
            ordinal += 1
            sources.append(SourceEntry(ordinal, path, declared_sha256, page_index, declared_size))
    return sources


def process_sources(
    context: StageContext,
    tree: RunTree,
    sources: list[SourceEntry],
    read_bytes: Callable[[str], bytes],
    *,
    policy: dict[str, str],
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
    if mode == APPROVAL_GATED_REAL_INGRESS:
        if data_policy is None:
            gate.enforce(approval=None, policy=None)
        approval = gate.load_approval(approval_reference, root=tree.root, policy=data_policy)
        gate.enforce(approval=approval, policy=data_policy)
    admitted = 0
    seen_sources: dict[str, tuple[str, int]] = {}
    cache: dict[str, bytes] = {}
    # Parsed containers, alongside the bytes they were parsed from: a multi-page
    # source is read once and parsed once, and every one of its pages is rendered
    # out of that single parse.
    documents: dict[str, Any] = {}

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

        actual_digest = digest_bytes(data)
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
        decision = decide(data, source, policy, documents=documents)

        if decision.outcome == "refused":
            _publish(
                context,
                source,
                outcome="refused",
                reason=decision.reason,
                approval_reference=approval_reference,
            )
            continue

        # Registered *after* the decision, and only on an admission: the duplicate
        # reason says "already admitted as source-N", and a record may only claim
        # what actually happened (GOVERNANCE 10). Registering before `decide` made
        # a refused source the "first", so a second copy of two corrupt files was
        # told its twin had been admitted, and the census read "one corrupt file,
        # one duplicate" when the truth was two corrupt files.
        seen_sources.setdefault(actual_digest, (source.declared_path, source.ordinal))
        _, published = tree.put_blob(DOOR, decision.store_bytes)
        extra: dict[str, Any] = {
            "sha256": decision.digest,
            "stored_at": published.relative_path,
            "geometry": {"width": decision.geometry[0], "height": decision.geometry[1]},
        }
        if source.pdf_page_index is not None:
            # The one recorded transform this door performs. ARCHITECTURE's third
            # invariant — the exact image shown to a model is reproducible from the
            # Exemplar plus recorded transforms — is only true if the render is
            # *recorded*: which page, of which file, produced these bytes. Without
            # it a sealed page's digest simply disagrees with its source's and
            # nothing can say why.
            extra["pdf_page_index"] = source.pdf_page_index
            # `container_sha256`, not `source_sha256`. The Exemplar's page payload
            # already uses `source_sha256` for the digest of the *sealed* bytes —
            # the one `page_id` binds, and the one `common/contracts/identities.py`
            # names — so a second meaning for the same word inside one record is
            # GLOSSARY's opening rule broken three lines apart, and a reader taking
            # the top-level field for "the file this came from" would be right for
            # every raster and wrong for every rendered page.
            extra["container_sha256"] = source.declared_sha256 or digest_bytes(data)
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
    payload: dict = {"declared_path": source.declared_path, "ordinal": source.ordinal}
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


def require_some_admitted(admitted: int, tree: RunTree) -> None:
    """An empty or wholly refused input set is a loud failure (harvest #3).

    Spec 03's test 2 asks for "zero admitted and a **named list**", and the name
    that matters is the *reason*: the old door's actual defect was an anonymous
    "unsupported" counter that told Tyrel a number and nothing he could act on. So
    the failure carries the census of closed-set reason codes actually published,
    read back off the door's own artifacts rather than off what this loop believed
    it wrote — a summary is never verification.

    It carries no declared path or filename. Those are recorded per source in the
    admission artifacts named below, which live inside the run tree under an
    approved storage root; the data-handling policy's logging rule keeps them out
    of anything an operator's shell captures.
    """
    if admitted != 0:
        return
    total, census = _refusal_census(tree)
    named = ", ".join(f"{code}: {count}" for code, count in sorted(census.items()))
    raise ContractError(
        f"the door admitted nothing: {total} source(s) submitted, "
        f"{sum(census.values())} refused ({named or 'no refusal was recorded either'}). "
        f"Each one is named by ordinal and declared path in "
        f"{writing_directory(DOOR)}/artifacts/admission/. An empty or wholly unreadable input "
        "set is a loud failure, never a green run with no output (harvest #3)"
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
        "--data-gate-policy",
        default=str(gate.DEFAULT_POLICY_PATH),
        help="the data-handling policy whose canonical hash the approval must name",
    )
    parser.add_argument(
        "--format-policy",
        default=str(admission.DEFAULT_FORMAT_POLICY_PATH),
        help="the admission list: which formats may enter at all",
    )
    args = parser.parse_args()
    registry = registry_factory(args.models_config)

    if args.submission_folder is not None:
        return real_submission(args, registry)
    if args.approval_record is not None:
        raise ContractError(
            "an approval record is meaningful only with a real submission folder; the "
            "walking skeleton's declared synthetic pages are not gated input"
        )
    return fixture_submission(args, registry)


def fixture_submission(args, registry) -> int:
    """The walking skeleton: declared synthetic pages, no gate, sealed as such."""
    fixture_root = declared_synthetic_fixture_root(args.fixture_root)
    fixture = load_fixture(str(fixture_root))
    scenario_for(fixture, args.scenario)
    declared = declared_digests(fixture, args.scenario)
    policy = admission.load_format_policy(Path(args.format_policy))
    bindings = run_config_bindings(registry.config, fixture, args.scenario)

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
    )
    context.finish(DOOR)
    require_some_admitted(admitted, tree)
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
    approval, reference = gate.read_external_approval(Path(args.approval_record), data_policy)

    roots = gate.approved_storage_roots(data_policy)
    # The *resolved* paths are used from here on, as `submit.py` does and for the
    # same reason: checking one path and then opening another is where a
    # check-then-use race lives, and the resolved values are already in hand.
    submission_folder = gate.require_approved_storage_location(
        Path(args.submission_folder), roots, "submitted folder"
    )
    gate.require_approved_storage_location(Path(args.run_root), roots, "run root")

    format_policy = admission.load_format_policy(Path(args.format_policy))
    found = inventory.read_submission(submission_folder, max_bytes=MAX_SOURCE_BYTES)
    if not found:
        raise ContractError(
            "the submit door found no files to admit; an empty folder is a loud failure, "
            "never a green run with no output (harvest #3)"
        )
    bytes_by_path = {source.relative_path: source.data for source in found}

    def read_bytes(relative_path: str) -> bytes:
        data = bytes_by_path.get(relative_path)
        if data is None:
            # The inventory kept an exact digest and size but not the bytes. The
            # declared-size branch refuses it before this reader is called; this is
            # the fail-closed fallback if those two records ever drift.
            raise OSError(f"source exceeds the {MAX_SOURCE_BYTES}-byte admission limit")
        return data

    sources = expand_sources(
        [
            {
                "relative_path": source.relative_path,
                "sha256": source.sha256,
                "bytes": source.size,
            }
            for source in found
        ],
        read_bytes,
        format_policy,
    )
    bindings = _real_bindings(registry.config, found, data_policy, reference, format_policy)
    tree = RunTree.create(
        Path(args.run_root),
        args.run_id,
        source_manifest=[
            {
                "relative_path": source.declared_path,
                "sha256": source.declared_sha256,
                "ordinal": source.ordinal,
            }
            for source in sources
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=approval_gated_real_ingress_record(gate.policy_hash(data_policy), reference),
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
        data_policy=data_policy,
    )
    context.finish(DOOR)
    require_some_admitted(admitted, tree)
    return EXIT_COMPLETE


def _real_bindings(models, found, data_policy, reference, format_policy) -> dict[str, Any]:
    """The sealed configuration facts for a real submission.

    The source manifest binds the bytes. The configuration digest binds everything
    else that shaped what the door did: the model roster, the data-handling policy
    version, the exact approval that admitted the corpus, and the admission list.
    A run resumed under a different approval or a different admission list is a
    different run wearing an old name, and `RunTree.create` refuses it before
    anything is written.
    """
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "submission": [
                    {"relative_path": source.relative_path, "sha256": source.sha256}
                    for source in found
                ],
                "data_gate_policy_hash": gate.policy_hash(data_policy),
                "data_gate_approval_ref": reference.to_record(),
                "format_policy": format_policy,
                "models": models.to_record(),
            }
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
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
