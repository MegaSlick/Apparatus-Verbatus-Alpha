"""Dissent on derived comparison views: equality only, never a distance
metric; a raw-string cross-check beside the normalized one; an honest
`"unknown"` for a witness format the comparator cannot yet reduce.
"""

import ast
import sys
import unicodedata
from pathlib import Path

import pytest

from common import dissent
from common.alignment import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    StepCountedMatcher,
    load_dissent_limits,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.prior_draft import COMPARISON_STEP_LIMIT_REASON, unmeasured_comparison
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree
from conftest import load_stage, programs_through, run_stage

ROOT = Path(__file__).resolve().parents[2]
# The sealed dissent budget, loaded, never a copy of it.
BUDGET = load_dissent_limits()[0].max_comparison_steps


def _dissent_against(reading, testimonia):
    return dissent.dissent_against(reading, testimonia, max_comparison_steps=BUDGET)


def test_comparison_view_collapses_whitespace_and_reports_what_it_dropped():
    view = dissent.comparison_view("alpha   beta\tgamma")
    assert view["normalized"] == "alpha beta gamma"
    assert view["dropped_characters"] == len("alpha   beta\tgamma") - len("alpha beta gamma")


def test_comparison_view_never_folds_case():
    """A case difference is a real disagreement about the ink, not a
    formatting artifact this normalization may erase."""
    view = dissent.comparison_view("Alpha")
    assert view["normalized"] == "Alpha"


def test_comparison_view_treats_precomposed_and_decomposed_accents_as_equal():
    """A precomposed 'e with acute' and a bare 'e' plus a combining acute
    render identically and are the same ink -- an OCR engine and a witness
    model are not guaranteed to agree on which Unicode form they emit for the
    same character, and parish-register French is full of exactly this."""
    precomposed = unicodedata.normalize("NFC", "baptisé")
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert precomposed != decomposed, "the fixture must actually differ at the codepoint level"
    assert (
        dissent.comparison_view(precomposed)["normalized"]
        == dissent.comparison_view(decomposed)["normalized"]
    )


def test_composing_an_accent_is_not_counted_as_a_dropped_character():
    """`dropped_characters` is the whitespace collapse's account and nothing
    else. NFC re-encodes a character, it does not remove one, so a decomposed
    witness report must show zero loss -- otherwise every diacritic-heavy act
    in a French parish register carries a loss figure that is simply wrong, and
    a reader weighing `departed` against it is misled on exactly the text this
    normalization was added for."""
    decomposed = unicodedata.normalize("NFD", "baptisé le premier février")
    composed = unicodedata.normalize("NFC", decomposed)
    assert len(decomposed) > len(composed), "the fixture must actually decompose"
    assert dissent.comparison_view(decomposed)["dropped_characters"] == 0

    # And a real collapse is still counted, over the composed string.
    assert dissent.comparison_view(f"{decomposed}  x")["dropped_characters"] == 1


def test_a_normalization_form_difference_alone_produces_no_dissent():
    reading = unicodedata.normalize("NFC", "baptisé le premier février")
    reported = unicodedata.normalize("NFD", reading)
    rows = _dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]
    )
    assert rows[0]["departed"] is False
    assert rows[0]["departed_raw"] is True, (
        "the raw strings really do differ codepoint-for-codepoint; only the "
        "normalized view is expected to treat them as the same ink"
    )


def test_a_witness_that_agrees_after_whitespace_normalization_departs_only_raw():
    testimonia = [
        {
            "outcome": "read",
            "payload": {"chair": "attestator_1", "reported": "alpha  beta gamma"},
        }
    ]
    rows = _dissent_against("alpha beta gamma", testimonia)
    assert rows == [
        {
            "chair": "attestator_1",
            "compared": True,
            "departed": False,
            "departed_raw": True,
            # The one whitespace character, located rather than merely counted.
            "departures": [
                {
                    "reading_span": {"start": 5, "end": 5},
                    "testimonium_span": {"start": 5, "end": 6},
                }
            ],
            "comparison_loss": {"reading_dropped_characters": 0, "witness_dropped_characters": 1},
        }
    ]


def test_a_witness_that_actually_disagrees_departs_on_both_views():
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamna"}}
    ]
    rows = _dissent_against("alpha beta gamma", testimonia)
    assert rows[0]["departed"] is True
    assert rows[0]["departed_raw"] is True


def test_departures_locate_the_one_character_the_witness_read_differently():
    """The instrument's whole point: one wrong letter in an otherwise identical
    reading must look different from wholesale disagreement, which one boolean
    per chair cannot express."""
    reading = "alpha beta gamma"
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamna"}}
    ]
    spans = _dissent_against(reading, testimonia)[0]["departures"]
    assert spans == [
        {"reading_span": {"start": 14, "end": 15}, "testimonium_span": {"start": 14, "end": 15}}
    ]
    assert reading[14:15] == "m"


def test_an_agreeing_witness_produces_no_departures_at_all():
    """Zero dissent on an easy line is the correct output, not a missing
    measurement -- ARCHITECTURE, verbatim: "a metric that rewards disagreement
    rewards hallucination"."""
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamma"}}
    ]
    assert _dissent_against("alpha beta gamma", testimonia)[0]["departures"] == []


def test_departures_are_an_alignment_and_expose_no_similarity_number():
    """The line this module must not cross. `get_opcodes` describes *where* two
    strings differ; `ratio()` is the metric a fuzzy-match picker would be built
    from, and no row may carry one."""
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "entirely other"}}
    ]
    row = _dissent_against("alpha beta gamma", testimonia)[0]
    for span in row["departures"]:
        assert set(span) == {"reading_span", "testimonium_span"}
        for bounds in span.values():
            assert set(bounds) == {"start", "end"}
            assert all(isinstance(value, int) for value in bounds.values())

    def scalars(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from scalars(item)
        elif isinstance(value, list):
            for item in value:
                yield from scalars(item)
        else:
            yield value

    assert not any(isinstance(value, float) for value in scalars(row)), (
        "a float anywhere in a dissent row is a similarity score wearing a shape; "
        "refusing ratios is what keeps the comparison from becoming a fuzzy-match picker"
    )


def test_perlector_and_archetypus_have_no_direct_sum_call_over_a_chair_expression():
    """Catch the direct cross-chair counter this narrow AST guard recognizes.

    A direct ``sum(...)`` whose expression names a chair would create a second
    denominator beside `witness_coverage`.  More indirect data flow still needs
    code review; this test does not claim to prove a whole-program property.
    """
    sources = [
        ROOT / "pipeline" / "4_perlector" / "run.py",
        ROOT / "pipeline" / "6_archetypus" / "run.py",
    ]
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "sum"
            ):
                if "chair" in ast.unparse(node):
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"cross-chair counters belong only in witness_coverage: {offenders}"


def test_downstream_stages_do_not_directly_subscript_a_dissent_field():
    """Catch direct ``[\"dissent\"]`` reads; indirect data flow remains review work."""
    sources = [
        ROOT / "pipeline" / "5_recensor" / "run.py",
        ROOT / "pipeline" / "6_archetypus" / "run.py",
    ]
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if isinstance(node.slice, ast.Constant) and node.slice.value == "dissent":
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"only Perlector may branch on dissent rows: {offenders}"


def test_a_non_reading_outcome_is_recorded_as_no_opinion_not_agreement():
    """Silence is not assent: a chair that failed or never ran gets
    `compared: False` with its outcome as the reason, never folded into the
    agreeing set."""
    for outcome in ("failed", "dead", "not-run", "excluded"):
        rows = _dissent_against(
            "reading", [{"outcome": outcome, "payload": {"chair": "attestator_3"}}]
        )
        assert rows == [{"chair": "attestator_3", "compared": False, "reason": outcome}]


def test_a_witness_whose_format_can_express_uncertainty_is_unknown_not_guessed():
    """A capability-declared chair with no act-anchored comparison view (R4's
    alignment) stays honestly unmeasurable -- forged directly onto a bare
    record, the same technique `test_testimonia_latest_attempt.py` already
    uses to exercise a boundary no live act-scoped producer reaches."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_2",
                "reported": "alpha [beta|beeta] gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        }
    ]
    rows = _dissent_against("alpha beta gamma", testimonia)
    assert rows == [
        {
            "chair": "attestator_2",
            "compared": "unknown",
            "reason": (
                "this witness's declared format cannot be reduced to a plain comparison view"
            ),
        }
    ]


def test_a_page_witness_comparison_view_lifts_the_capability_exemption():
    """The same capability-declared format becomes measurable once R4's
    alignment hands it an act-anchored, markup-stripped `comparison_reported`
    view -- the exemption is about an unsafe raw report, not the chair."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_2",
                "reported": "alpha [beta|beeta] gamma",
                "comparison_reported": "alpha beta gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        }
    ]
    rows = _dissent_against("alpha beta gamma", testimonia)
    assert rows[0]["compared"] is True
    # The point of the exemption lift: the act-anchored `comparison_reported`
    # view is what gets diffed. Diffing the raw report ("alpha [beta|beeta]
    # gamma") would leave `compared` True and only these lines red -- and would
    # record every act with alternative-reading markup as dissenting when the
    # witness in fact agreed.
    assert rows[0]["departed"] is False, rows[0]
    assert rows[0]["departures"] == []
    assert "reason" not in rows[0]


def test_a_bracket_marker_view_lifts_the_exemption_for_an_act_scoped_chair():
    """The act-scoped half of the same lift, on the notation DAI actually uses.

    `markup_text_view` removes TAG markup, so it does nothing to a bracketed
    `[UNCERTAIN]`. An act-scoped chair declaring `can_express_uncertainty`
    therefore had no safe view at all and stayed `compared: "unknown"` forever
    -- the parroting instrument dark on the one chair whose grammar says most
    about uncertain ink. `common/alignment.py::bracket_marker_view`, wired in by
    `pipeline/4_perlector/run.py::dissent_testimonia`, is the view that lifts
    it, and the counterfactual below is why the strip has to happen at all.
    """
    raw = "Marie [UNCERTAIN] Dupont"
    stripped = "Marie  Dupont"
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_2",
                "payload": raw,
                "comparison_reported": stripped,
                "format_capabilities": {"can_express_uncertainty": True},
            },
        }
    ]
    rows = _dissent_against("Marie Dupont", testimonia)
    assert rows[0]["compared"] is True
    # The bracket marker is gone, so what remains is a whitespace difference:
    # `departed` (normalized) is False while `departed_raw` still records that
    # the strings were not byte-equal. Two honest answers to two questions.
    assert rows[0]["departed"] is False, rows[0]
    assert rows[0]["departed_raw"] is True, rows[0]

    # The counterfactual: without the strip, this chair reads as dissenting
    # about eleven characters it never disagreed about.
    raw_rows = _dissent_against(
        "Marie Dupont",
        [
            {
                "outcome": "read",
                "payload": {
                    "chair": "attestator_2",
                    "payload": raw,
                    "comparison_reported": raw,
                    "format_capabilities": {"can_express_uncertainty": True},
                },
            }
        ],
    )
    assert raw_rows[0]["compared"] is True
    assert raw_rows[0]["departed"] is True, raw_rows[0]
    assert raw_rows[0]["departures"], raw_rows[0]


def test_a_runaway_witness_report_is_unknown_rather_than_aligned():
    """A witness's `reported` is a model's own output and nothing upstream bounds
    it. A model stuck in a repetition loop until its token cap produces a pair
    far past the pair prefilter, which refuses it before the matcher runs. The
    bound is on the comparison, not the text: neither string is clipped, and
    the chair keeps a visible row."""
    reading = "alpha beta gamma" * 700  # a ~11k-character act, already unrealistic
    runaway = "ab" * 60_000  # ~120k characters, a plausible 32k-token repetition loop
    assert len(reading) * len(runaway) > dissent.MAX_COMPARISON_CHARACTER_PAIRS

    # No timing assert: the prefilter rejects before the matcher runs, and the
    # functional asserts below would fail if it stopped firing.
    rows = _dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": runaway}}]
    )
    assert rows[0]["chair"] == "attestator_1", "the witness must not vanish from the record"
    assert rows[0]["compared"] == "unknown"
    assert "did not run" in rows[0]["reason"]


def test_a_long_but_affordable_comparison_is_still_genuinely_aligned():
    """Prove the bound is not swallowing ordinary work: an act far longer than a
    real register entry still gets its real spans."""
    reading = "alpha beta gamma " * 200
    reported = reading[:-6] + "gamna "
    rows = _dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]
    )
    assert rows[0]["compared"] is True
    assert rows[0]["departures"], "an affordable comparison must still locate its departures"


def _steps_to_compare(reading: str, reported: str) -> int:
    matcher = StepCountedMatcher(reading, reported, 10**12)
    matcher.get_opcodes()
    return 10**12 - matcher.steps_left


def test_a_comparison_under_the_pair_bound_stops_on_its_exact_step_count():
    """The pair prefilter admits comparisons whose matcher work runs far past
    the pair count, so the step budget is what stops them. It is a count: the
    same pair is compared with exactly the steps it needs and is `unknown` with
    one fewer, on any machine and under any load, and the row records the
    budget it ran out of."""
    reading = "alpha beta gamma " * 400
    reported = "alpha beta gamna " * 400
    assert len(reading) * len(reported) < dissent.MAX_COMPARISON_CHARACTER_PAIRS
    needed = _steps_to_compare(reading, reported)
    testimonia = [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]

    rows = dissent.dissent_against(reading, testimonia, max_comparison_steps=needed)
    assert rows[0]["compared"] is True

    rows = dissent.dissent_against(reading, testimonia, max_comparison_steps=needed - 1)
    assert rows[0]["chair"] == "attestator_1", "the witness must not vanish from the record"
    assert rows[0]["compared"] == "unknown"
    assert rows[0]["max_comparison_steps"] == needed - 1
    assert f"the sealed {needed - 1}-step dissent budget" in rows[0]["reason"]
    dissent.validate_dissent(
        rows,
        text=reading,
        basis_testimonia=[{"chair": "attestator_1", "outcome": "read"}],
    )


def test_departures_past_the_budget_is_an_explicit_non_verdict_not_an_agreement():
    """`self_revision` is measured by `departures`. A comparison the budget
    stops must never read as "no revisions", which `[]` means, so it returns
    the closed non-verdict naming the budget instead."""
    reading, prior = "alpha beta gamma " * 50, "alpha beta gamna " * 50
    needed = _steps_to_compare(reading, prior)

    assert dissent.departures(reading, prior, needed)
    assert dissent.departures(reading, prior, needed - 1) == unmeasured_comparison(needed - 1)
    assert unmeasured_comparison(needed - 1) == {
        "measured": False,
        "reason": COMPARISON_STEP_LIMIT_REASON,
        "max_comparison_steps": needed - 1,
    }


def test_a_budget_is_recorded_only_on_an_unknown_row():
    """A compared-row budget or a malformed one is refused, so the field means
    one thing: this comparison ran out of that many steps."""
    basis = [{"chair": "attestator_1", "outcome": "read"}]
    stopped = {"chair": "attestator_1", "compared": "unknown", "reason": "stopped"}
    dissent.validate_dissent(
        [{**stopped, "max_comparison_steps": 5}], text="", basis_testimonia=basis
    )
    for budget in (0, True, "5"):
        with pytest.raises(SchemaRefusal, match="uncomputed-row schema"):
            dissent.validate_dissent(
                [{**stopped, "max_comparison_steps": budget}], text="", basis_testimonia=basis
            )
    # A row that compared, and a row for a witness that never reported, have no
    # budget to have run out of.
    (compared,) = _dissent_against(
        "alpha", [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": "alpha"}}]
    )
    assert compared["compared"] is True
    dissent.validate_dissent([compared], text="alpha", basis_testimonia=basis)
    with pytest.raises(SchemaRefusal, match="closed compared-row schema"):
        dissent.validate_dissent(
            [{**compared, "max_comparison_steps": 5}], text="alpha", basis_testimonia=basis
        )
    silent = [{"chair": "attestator_1", "outcome": "failed"}]
    unreported = {"chair": "attestator_1", "compared": False, "reason": "failed"}
    dissent.validate_dissent([unreported], text="", basis_testimonia=silent)
    with pytest.raises(SchemaRefusal, match="uncomputed-row schema"):
        dissent.validate_dissent(
            [{**unreported, "max_comparison_steps": 5}], text="", basis_testimonia=silent
        )


def test_the_perlector_compares_under_the_sealed_dissent_budget(tmp_path):
    """The budget is `config/alignment.toml`'s `[dissent] max_comparison_steps`,
    sealed with the run and passed to every comparison, with no copy of it in
    this module to drift from the config. A run sealed with a one-step budget
    records every non-trivial comparison as stopped on exactly that budget."""
    assert not hasattr(dissent, "MAX_COMPARISON_STEPS")
    shipped = DEFAULT_ALIGNMENT_CONFIG_PATH.read_text(encoding="utf-8")
    sealed_line = f"max_comparison_steps = {BUDGET}\n"
    assert shipped.count(sealed_line) == 1
    config = tmp_path / "alignment.toml"
    config.write_text(shipped.replace(sealed_line, "max_comparison_steps = 1\n"), encoding="utf-8")
    root = tmp_path / "runs"
    for program in programs_through("perlector"):
        result = run_stage(root, "dissent-budget", "happy", program, alignment_config=config)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "dissent-budget")
    rows = [
        row
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
        for row in tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])["payload"][
            "dissent"
        ]
    ]
    stopped = [row for row in rows if "max_comparison_steps" in row]
    assert stopped, "a one-step budget stopped no comparison"
    assert all(row["compared"] == "unknown" for row in stopped)
    assert {row["max_comparison_steps"] for row in stopped} == {1}
    assert not [row for row in rows if row["compared"] is True and row["departures"]]


def test_the_perlector_refuses_a_dissent_budget_the_run_never_sealed(tmp_path, monkeypatch):
    """The run seals the shipped `config/alignment.toml`, and the stage's binding
    check reads that file when the pass opens. If the budget the Perlector then
    loads differs -- the file changed between the two reads -- it must refuse,
    not compare under a budget the run never bound. Run in process so the
    second read alone sees the change."""
    shipped = DEFAULT_ALIGNMENT_CONFIG_PATH.read_text(encoding="utf-8")
    sealed_line = f"max_comparison_steps = {BUDGET}\n"
    changed = tmp_path / "alignment.toml"
    changed.write_text(
        shipped.replace(sealed_line, f"max_comparison_steps = {BUDGET + 1}\n"), encoding="utf-8"
    )
    root = tmp_path / "runs"
    for program in programs_through("attestatores"):
        result = run_stage(root, "r", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    perlector = load_stage("4_perlector")
    monkeypatch.setattr(
        perlector, "load_dissent_limits", lambda _path: load_dissent_limits(changed)
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(ROOT / "pipeline/4_perlector/run.py"), "--run-root", str(root), "--run-id", "r"]
        + ["--scenario", "happy"],
    )

    with pytest.raises(ContractError, match="the alignment configuration changed between"):
        perlector.main()
    assert not RunTree(root, "r").build_manifest(PERLECTOR)["artifacts"]


def test_is_comparable_defaults_true_when_a_testimonium_declares_no_capabilities():
    assert dissent.is_comparable({"payload": {"format_capabilities": {}}}) is True
    assert dissent.is_comparable({"payload": {}}) is True


def test_dissent_never_drops_an_unknown_chair_from_the_record():
    """An incomparable witness must stay visible in the list -- never silently
    absent, which would look identical to a chair the run never configured."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_1",
                "reported": "alpha beta gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        },
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamma"}},
    ]
    rows = _dissent_against("alpha beta gamma", testimonia)
    chairs = {row["chair"] for row in rows}
    assert chairs == {"attestator_1", "attestator_2"}
    unknown_row = next(row for row in rows if row["chair"] == "attestator_1")
    assert unknown_row["compared"] == "unknown"


def test_a_completed_structured_witness_stays_visible_but_cannot_be_compared():
    assert _dissent_against(
        "reading",
        [
            {
                "outcome": "read",
                "payload": {
                    "chair": "attestator_1",
                    "payload": {"blocks": [{"text": "retained without coercion"}]},
                },
            }
        ],
    ) == [
        {
            "chair": "attestator_1",
            "compared": "unknown",
            "reason": "no comparable text for this act: retained derived testimony is not text",
        }
    ]


def test_a_completed_reading_outcome_carrying_no_text_at_all_is_unknown_not_a_refusal():
    """The textless case of the branch above, which used to be a `SchemaRefusal`.

    Before the retained-native seam a completed reading with nothing to compare
    raised "no text to compare", because a missing `reported` field could only
    mean a malformed record. It no longer can: the comparison text is the
    retained payload, and a witness may genuinely hold one that is not text. So
    the chair is recorded `compared: "unknown"` with the fact named, exactly as
    a structured payload is -- visible, uncounted toward the floor, and neither
    guessed at nor dropped. Pinned separately from the structured
    case because an absent payload and an object payload are different records
    reaching the same branch, and only one of them was ever exercised here.
    """
    assert _dissent_against(
        "reading", [{"outcome": "read", "payload": {"chair": "attestator_1"}}]
    ) == [
        {
            "chair": "attestator_1",
            "compared": "unknown",
            "reason": "no comparable text for this act: retained derived testimony is not text",
        }
    ]


def test_this_module_pins_equality_only_and_takes_no_similarity_parameter():
    """Structural pin, not merely a docstring promise: `comparison_view` must
    never grow a threshold, weight, or distance-metric parameter. A reviewer
    changing this file should find this test and refuse the change rather
    than update it to match."""
    import inspect

    parameters = inspect.signature(dissent.comparison_view).parameters
    assert list(parameters) == ["text"], (
        "comparison_view must take no per-chair parameter and no similarity "
        "threshold -- 'closest match' needs a metric, and refusing metrics is "
        "what keeps a normalization from becoming a fuzzy-match picker"
    )


# --- P2 review: the two halves of comparison_loss answer the same question ---


def test_a_decomposed_witness_report_is_not_charged_a_character_per_accent():
    """`comparison_view`'s docstring settles what `dropped_characters` counts:
    "NFC discards nothing -- it re-encodes a character, it does not remove
    one", and charging composition to the loss account "would put a wrong
    number on every diacritic-heavy act in the corpus this project exists to
    read". The witness half of `comparison_loss` must answer that same
    question: summing every `markup_text_view` loss field folded in its
    `unicode_reencoded_characters`, so a witness reporting decomposed French
    was recorded as losing one character per accent while the
    identically-composed reading was recorded as losing none.
    """
    precomposed = unicodedata.normalize("NFC", "baptisé et présenté")
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert precomposed != decomposed, "the fixture must actually differ at the codepoint level"

    rows = _dissent_against(
        precomposed,
        [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": decomposed}}],
    )

    assert rows[0]["departed"] is False, "the same ink in another normal form is not dissent"
    assert rows[0]["comparison_loss"] == {
        "reading_dropped_characters": 0,
        "witness_dropped_characters": 0,
    }


def test_markup_and_collapsed_whitespace_stay_in_the_witness_loss_account():
    """The other direction: tags and a collapsed run are genuine removals, and
    dropping them from the account would hide what the comparison view discarded.
    """
    rows = _dissent_against(
        "alpha beta",
        [
            {
                "outcome": "read",
                "payload": {"chair": "attestator_1", "reported": "<b>alpha   beta</b>"},
            }
        ],
    )

    assert rows[0]["departed"] is False
    assert rows[0]["comparison_loss"]["witness_dropped_characters"] == len("<b>") + len("</b>") + 2
