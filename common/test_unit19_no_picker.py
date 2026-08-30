"""§7 forbidden shapes, held against Unit 19A's identity surface.

The consult places the Unit 19 static guards at `pipeline/test_unit19_no_picker.py`
because §8.6's tests 52-67 guard the Perlector, Recensor, Archetypus and Armarium
stages that 19B-19D still have to build. Unit 19A's whole surface is in `common/`,
so its guards live beside it: a file under `pipeline/` that read only `common/`
sources would misname what it covers, and would read as though §8.6 were populated
when fifteen of its sixteen tests are still owed.

Two kinds of guard, because neither is sufficient alone. The source scan catches a
selection idiom that has not been reached by a test yet; the permutation tests
catch a positional decision that no idiom names -- an insertion-order dict, a
first-match loop, a set iteration that happens to be stable.
"""

import ast
import itertools
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.errors import IdentityRefusal, SchemaRefusal
from common.contracts.identities import act_id, physical_act_component_designation
from common.corpus_register import (
    _FORBIDDEN_PREFERENCE_FIELDS,
    append_records,
    empty_register,
    register_digest,
)
from common.physical_act_partition import (
    build_correspondence_proposal,
    build_physical_act_partition,
)
from common.test_unit19_physical_act_partition import (
    PAGE,
    PG1,
    PG2,
    PG3,
    SEAL,
    SOURCE_A,
    SOURCE_B,
    SOURCE_C,
    _align,
    _local,
    _mint,
    _three_member_register,
)

PARTITION_SOURCE = Path(__file__).with_name("physical_act_partition.py")

# Consult §7 shape 1, in full. These are binding review vocabulary, so the
# recursive screen spells every one of them rather than the subset that happened
# to appear in a payload first.
SHAPE_ONE_WORDS = frozenset(
    {"primary", "canonical", "preferred", "best", "better", "winner", "selected", "chosen"}
)

# Consult §7 shapes 2, 3 and 11: position, extremum, and reproducible arbitrariness.
FORBIDDEN_CALLS = frozenset({"next", "min", "max", "choice", "shuffle", "sample", "randint"})


def test_the_recursive_preference_screen_names_every_shape_one_word():
    assert SHAPE_ONE_WORDS <= _FORBIDDEN_PREFERENCE_FIELDS


def test_the_correspondence_module_contains_no_positional_or_extremal_selection():
    """§7 shapes 2, 3 and 11 at the module that decides logical act identity.

    A blanket ban is honest here because nothing in this module needs one of
    these calls: every set it holds is either serialized whole (`sorted`) or
    proved to have exactly one member before it is unpacked. The corpus
    register's `_retract_membership` does use `next` and `chain[-1]`, and that is
    chain surgery over a structure whose order *is* the declared fact -- it
    selects nothing among captures, readings, or acts.
    """
    tree = ast.parse(PARTITION_SOURCE.read_text())
    # Attribute call targets count too. Matching only `ast.Name` left
    # `random.choice(...)`, `statistics.mode(...)` and `helper.max(...)`
    # invisible -- three of the plainest ways to pick a winner among captures,
    # walking past the last automated screen before a chosen reading becomes a
    # durable corpus mint.
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert not called & FORBIDDEN_CALLS, sorted(called & FORBIDDEN_CALLS)
    # And a negative index. `chain[-1]` parses as a unary minus over a constant,
    # not a constant, so the shape this test's own docstring names as the
    # example was the one shape it did not collect.
    subscripts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and (
            (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int))
            or (
                isinstance(node.slice, ast.UnaryOp)
                and isinstance(node.slice.op, ast.USub)
                and isinstance(node.slice.operand, ast.Constant)
                and isinstance(node.slice.operand.value, int)
            )
        )
    ]
    assert subscripts == [], [ast.unparse(node) for node in subscripts]


def test_no_textual_or_scoring_field_is_read_by_the_correspondence_module():
    """§7 shapes 4 and 12. Physical acts are matched by geometry: there is no

    number in this surface to rank by and no string to compare. The scan reads
    identifiers and the record fields the module actually reads, not its prose --
    `_TEXTUAL_FIELDS` names `edit_distance` in order to refuse it, and a guard
    that could not tell a refusal from a use would have to be switched off.
    """
    tree = ast.parse(PARTITION_SOURCE.read_text())
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            read.add(node.id)
        elif isinstance(node, ast.Attribute):
            read.add(node.attr)
        elif isinstance(node, ast.arg):
            read.add(node.arg)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            read.add(node.slice.value)
        elif isinstance(node, ast.Call):
            # A field can be read through an argument rather than a subscript:
            # `component.get("ocr")` and `row.pop("text", None)` put the banned
            # name where neither the subscript branch nor the name branch looks.
            # That is precisely the shape this guard exists to stop, because
            # this module decides durable physical-act identity and a
            # text-derived match here turns a witness reading into a corpus mint.
            read.update(
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            )
    banned = {
        "confidence",
        "iou",
        "overlap",
        "residual",
        "score",
        "sharpness",
        "legibility",
        "edit_distance",
        "text",
        "ocr",
        "testimonium",
        "lectio",
        "perlectio",
    }
    assert not read & banned, sorted(read & banned)


def test_the_component_designation_cannot_be_bent_into_a_ranking():
    """A mint designation is a set. Any ordering a caller supplies is refused

    rather than honoured, so 'the first act in the component' can never become
    the seed the physical act is named after.
    """
    ids = sorted([act_id(PG1, "proposal", {"x": 0, "y": y, "w": 10, "h": 10}) for y in (0, 20)])
    assert isinstance(physical_act_component_designation(PAGE, ids), str)
    for bad in (list(reversed(ids)), [*ids, ids[0]], [], ["act_not-a-derived-identity"]):
        with pytest.raises(IdentityRefusal, match="sorted unique act ids"):
            physical_act_component_designation(PAGE, bad)
    with pytest.raises(IdentityRefusal, match="physical page id"):
        physical_act_component_designation("ppg_not-a-derived-identity", ids)


def _adversarial_case(tmp_path):
    """Three captures of one physical page, plus one act on an unclustered one.

    A shared act across two captures, an act only one capture proposed, an act
    whose correspondence does not exist (held), and a genuinely image-local act.
    """
    path, _head = _three_member_register(tmp_path)
    shared_a = act_id(PG1, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    shared_b = act_id(PG2, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    only_c = act_id(PG3, "proposal", {"x": 0, "y": 30, "w": 10, "h": 10})
    held = act_id(PG1, "proposal", {"x": 0, "y": 60, "w": 10, "h": 10})
    local_page = "pg_" + "9" * 16
    unclustered = "d" * 64
    solo = act_id(local_page, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    _mint(
        path, PAGE, [_local(shared_a, PG1, SOURCE_A, "s1"), _local(shared_b, PG2, SOURCE_B, "s2")]
    )
    _mint(
        path,
        PAGE,
        [_local(only_c, PG3, SOURCE_C, "c1", {"x": 0, "y": 30, "w": 10, "h": 10})],
        run="run-2",
    )
    acts = [
        _local(shared_a, PG1, SOURCE_A, "s1"),
        _local(shared_b, PG2, SOURCE_B, "s2"),
        _local(only_c, PG3, SOURCE_C, "c1", {"x": 0, "y": 30, "w": 10, "h": 10}),
        _local(held, PG1, SOURCE_A, "h1", {"x": 0, "y": 60, "w": 10, "h": 10}),
        _local(solo, local_page, unclustered, "l1"),
    ]
    alignments = [
        _align(PG1, SOURCE_A, PAGE, "align:a"),
        _align(PG2, SOURCE_B, PAGE, "align:b"),
        _align(PG3, SOURCE_C, PAGE, "align:c"),
    ]
    return path, acts, alignments, {SOURCE_A, SOURCE_B, SOURCE_C, unclustered}


def test_the_partition_is_identical_under_every_submission_order(tmp_path):
    """§7 shapes 2 and 14, behaviourally. Neither the order acts were proposed

    in nor the order captures were aligned in may reach the sealed bytes.
    """
    path, acts, alignments, ledger = _adversarial_case(tmp_path)
    register_bytes = path.read_bytes()

    def build(local_acts, capture_alignments):
        return build_physical_act_partition(
            register=register_bytes,
            register_digest=register_digest(register_bytes),
            proposal_seal_ref=SEAL,
            local_acts=list(local_acts),
            capture_alignments=list(capture_alignments),
            source_ledger=ledger,
        )

    expected = build(acts, alignments)
    assert expected["logical_expected_count"] == 3
    assert expected["local_expected_count"] == 5
    assert len(expected["findings"]) == 1
    for act_order in itertools.permutations(acts):
        assert build(act_order, alignments) == expected
    for alignment_order in itertools.permutations(alignments):
        assert build(acts, alignment_order) == expected


def test_proposal_reference_enumeration_cannot_reach_partition_bytes():
    """Several regions of one local act are a set, not an ordering channel."""
    local = _local(
        act_id(PG1, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10}),
        PG1,
        SOURCE_A,
        "a",
    )
    refs = ["proposal:z", "proposal:a", "proposal:m"]

    def build(order):
        return build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref=SEAL,
            local_acts=[{**local, "proposal_refs": list(order)}],
            capture_alignments=[],
            source_ledger={SOURCE_A},
        )

    expected = build(refs)
    for order in itertools.permutations(refs):
        assert build(order) == expected


def test_the_correspondence_proposal_is_identical_under_every_component_order(tmp_path):
    """The consult requires that changing enumeration order reproduce identical

    correspondence evidence. Component order reached the sealed `self_hash` and
    the sequence the register was asked to append, so two runs that found the
    same components in a different order gave the corpus a different digest for
    identical evidence.
    """
    path, _head = _three_member_register(tmp_path)
    register_bytes = path.read_bytes()
    first = act_id(PG1, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    second = act_id(PG2, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    third = act_id(PG3, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    components = [
        {
            "physical_page_id": PAGE,
            "physical_act_id": None,
            "local_acts": [_local(first, PG1, SOURCE_A, "a")],
            "evidence": ["geometry:one"],
            "finding": None,
        },
        {
            "physical_page_id": PAGE,
            "physical_act_id": None,
            "local_acts": [_local(second, PG2, SOURCE_B, "b")],
            "evidence": ["geometry:two"],
            "finding": None,
        },
        {
            "physical_page_id": PAGE,
            "physical_act_id": None,
            "local_acts": [_local(third, PG3, SOURCE_C, "c")],
            "evidence": ["geometry:three"],
            "finding": None,
        },
    ]

    def build(order):
        return build_correspondence_proposal(
            register=register_bytes,
            register_digest=register_digest(register_bytes),
            discovery_run_id="discovery",
            components=list(order),
        )

    expected = build(components)
    assert len(expected["accepted_records"]) == 6
    for order in itertools.permutations(components):
        assert build(order) == expected


def test_correspondence_evidence_and_member_enumeration_cannot_reach_the_seal(tmp_path):
    """F6 must cover the order *inside* a component as well as components.

    Registration evidence is a set of digest-bound facts.  If its caller order
    reaches the proposal, the same geometric component gives the discovery run
    a different seal and gives the corpus register different durable bytes.
    Member order is exercised alongside it so the two-pass resolver's entire
    enumeration surface is covered by one adversarial product.
    """
    path, _head = _three_member_register(tmp_path)
    register_bytes = path.read_bytes()
    acts = [
        _local(
            act_id(page, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10}),
            page,
            source,
            key,
        )
        for page, source, key in (
            (PG1, SOURCE_A, "a"),
            (PG2, SOURCE_B, "b"),
            (PG3, SOURCE_C, "c"),
        )
    ]
    evidence = ["geometry:a-b", "geometry:a-c", "geometry:b-c"]

    def build(member_order, evidence_order):
        return build_correspondence_proposal(
            register=register_bytes,
            register_digest=register_digest(register_bytes),
            discovery_run_id="discovery",
            components=[
                {
                    "physical_page_id": PAGE,
                    "physical_act_id": None,
                    "local_acts": list(member_order),
                    "evidence": list(evidence_order),
                    "finding": None,
                }
            ],
        )

    expected = build(acts, evidence)
    for member_order in itertools.permutations(acts):
        for evidence_order in itertools.permutations(evidence):
            assert build(member_order, evidence_order) == expected


def test_a_proposal_sealed_in_one_order_appends_in_an_order_the_register_reads(tmp_path):
    """Canonical record order is only safe if a mint still precedes every

    correspondence that names it: the register refuses a correspondence whose
    physical act nothing earlier declared.
    """
    path, _head = _three_member_register(tmp_path)
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(
                        act_id(PG2, "proposal", {"x": 0, "y": 0, "w": 1, "h": 1}),
                        PG2,
                        SOURCE_B,
                        "b",
                        {"x": 0, "y": 0, "w": 1, "h": 1},
                    )
                ],
                "evidence": ["geometry:two"],
                "finding": None,
            },
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(
                        act_id(PG1, "proposal", {"x": 0, "y": 0, "w": 1, "h": 1}),
                        PG1,
                        SOURCE_A,
                        "a",
                        {"x": 0, "y": 0, "w": 1, "h": 1},
                    )
                ],
                "evidence": ["geometry:one"],
                "finding": None,
            },
        ],
    )
    kinds = [row["kind"] for row in proposal["accepted_records"]]
    assert kinds == ["physical-act", "physical-act", "correspondence", "correspondence"]
    append_records(
        path,
        proposal["accepted_records"],
        expected_digest=register_digest(path.read_bytes()),
    )


def test_the_empty_register_still_refuses_a_preference_field():
    with pytest.raises(SchemaRefusal, match="preference"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": digest_bytes(b"seal")},
            local_acts=[
                {
                    **_local(
                        act_id(PG1, "proposal", {"x": 0, "y": 0, "w": 1, "h": 1}),
                        PG1,
                        SOURCE_A,
                        "a",
                    ),
                    "winner": True,
                }
            ],
            capture_alignments=[],
            source_ledger=set(),
        )
