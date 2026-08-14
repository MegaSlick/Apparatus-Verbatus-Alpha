"""The blanket empty-Testimonium branch is reached by identity, never by key.

`run.py::_is_page_fallback` decides, for every act in the denominator, whether
this stage skips `testimony_for` entirely and publishes `genuinely-empty` for
every configured chair. That is the one branch here that produces a completed
witness outcome without consulting anything the fixture declared about the act,
so what selects it has to be unforgeable. Its own docstring says so — "recognize
the reserved minted identity, not merely its human-readable key" — and the
Perlector's sibling branch (`4_perlector/reader.py::_is_page_fallback`) carries
two tests holding that claim down. This is the missing half: nothing here failed
when the derived-identity check was replaced with
`act_key.startswith("page-fallback:")`.

The consequence of the key form is concrete rather than theoretical. `act_key`
is a review-facing label; `fallback_page_act_key` is the only thing that
promises to spell it that way, and a fixture act, a hand-edited seal row, or a
future minter may carry the same string. Under a key match every witness would
report a completed empty read of a real act that nobody looked at, which is
GOALS 1's worst failure arriving as a green run.
"""

import importlib.util
from pathlib import Path

from common.contracts.identities import act_id as derive_act_id
from common.stage import FALLBACK_PAGE_ACT_ORDINAL, fallback_page_act_key

ROOT = Path(__file__).resolve().parents[2]


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_fallback_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attestatores = _load_attestatores()

PAGE_ID = "pg_0123456789abcdef"
FIXTURE = {"page": [{"ordinal": 1, "width": 200, "height": 260}]}
PAGE_BOUNDS = {"x": 0, "y": 0, "w": 200, "h": 260}


class _Tree:
    def __init__(self, record=None):
        self.record = record

    def build_manifest(self, stage):
        assert stage == attestatores.DESIGNATOR
        if self.record is None:
            return {"artifacts": []}
        return {
            "artifacts": [
                {
                    "kind": "page-fallback",
                    "subject_id": self.record["subject_id"],
                    "artifact_id": "fallback-record",
                }
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        assert (stage, kind, artifact_id) == (
            attestatores.DESIGNATOR,
            "page-fallback",
            "fallback-record",
        )
        return self.record


class _Context:
    def __init__(self, *, fixture=FIXTURE, act_id=None, page_bounds=PAGE_BOUNDS):
        self.fixture = fixture
        record = (
            {
                "subject_id": act_id,
                "payload": {"page_bounds": page_bounds},
            }
            if act_id is not None
            else None
        )
        self.tree = _Tree(record)


def _act(act_id: str, act_key: str) -> dict:
    return {"act_id": act_id, "act_key": act_key, "page_id": PAGE_ID, "page_ordinal": 1}


def test_the_reserved_minted_identity_is_recognized():
    """The real minted act still takes the branch it exists for."""
    minted = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, PAGE_BOUNDS)
    assert attestatores._is_page_fallback(
        _Context(act_id=minted), _act(minted, fallback_page_act_key(1))
    )


def test_a_fallback_shaped_key_cannot_blank_an_ordinary_act():
    """An ordinary act wearing the label is read, not declared empty unseen."""
    ordinary = derive_act_id(PAGE_ID, 0, {"x": 10, "y": 10, "w": 40, "h": 40})
    assert not attestatores._is_page_fallback(_Context(), _act(ordinary, fallback_page_act_key(1)))


def test_the_minted_identity_is_recognized_whatever_the_key_says():
    """And the reverse: identity decides, so a drifted label changes nothing."""
    minted = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, PAGE_BOUNDS)
    assert attestatores._is_page_fallback(_Context(act_id=minted), _act(minted, "a1"))


def test_the_identity_is_bound_to_this_page_rather_than_any_page():
    """A fallback identity minted over another page is not this page's."""
    other_page = derive_act_id("pg_fedcba9876543210", FALLBACK_PAGE_ACT_ORDINAL, PAGE_BOUNDS)
    assert not attestatores._is_page_fallback(
        _Context(act_id=other_page), _act(other_page, fallback_page_act_key(1))
    )


def test_recognition_uses_the_designator_record_when_fixture_dimensions_diverge():
    """Fixture declarations do not get a second vote on the sealed page rectangle."""
    minted = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, PAGE_BOUNDS)
    divergent_fixture = {"page": [{"ordinal": 1, "width": 199, "height": 259}]}

    assert attestatores._is_page_fallback(
        _Context(fixture=divergent_fixture, act_id=minted),
        _act(minted, fallback_page_act_key(1)),
    )
