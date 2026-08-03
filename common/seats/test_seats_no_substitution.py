"""Spec 02, test 7 — No substitution. The one this whole system exists for.

"Every refusal in the clause above, injected: each raises with the seat named,
and no other configured seat is invoked. This is the test the picker warning
never had."

The clause names seven doors, and each is a refusal and never a substitution:

  1. a seat whose digest does not verify
  2. a seat that will not resolve — never a fake, never a base, never a
     neighbouring revision
  3. a cache holding a different revision than the pin (harvest #43: a pin is a
     constant the artifact must MATCH, never a value the artifact supplies)
  4. an adapter that will not fetch — never the bare base under the adapter's
     seat name
  5. a serving recipe that will not start — never a second route under the same
     role name
  6. a seat configured `absent`, which stays in the roster as an explicit
     absence rather than being quietly filled
  7. a local-repository path that escapes its model root

Every roster below carries at least one other configured seat, so "no other
configured seat is invoked" is a real claim rather than a vacuous one, and every
door is driven through the *real* registry. Two logs are asserted on: the fetch
seam's, and a trace of every `resolve`/`ensure`/`receipt` call the registry made
while handling the refusal.

GOVERNANCE 3, read as ops rather than as models: nothing about a picker requires
that it be called one, or that a model be the thing doing the choosing.
"""

import json

import pytest

from common.seats.errors import (
    AdapterFetchRefusal,
    CacheRevisionRefusal,
    ChairRefusal,
    DigestMismatchRefusal,
    LocalPathRefusal,
    ServingRecipeRefusal,
    UnresolvedChairRefusal,
)
from common.seats.models import ChairIdentity
from common.seats.registry import CACHE_DESCRIPTOR, ChairRegistry

from .conftest import (
    RecordingFetcher,
    absent_chair,
    config_of,
    hf_chair,
    local_chair,
    pin_snapshot,
    registry_for,
    write_snapshot,
)

BYSTANDER = "attestator_2"
FILES = {"weights.bin": b"expected fixture bytes\n"}


class Traced:
    """Every protocol call the registry makes, in order, with the role asked for.

    A refusal that named the right seat could still have reached for another one
    on the way — resolving it, fetching it, receipting it — and the raised error
    would look identical. This is what makes the difference visible.
    """

    def __init__(self, registry: ChairRegistry):
        self._registry = registry
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name):
        return getattr(self._registry, name)

    def resolve(self, role: str):
        self.calls.append(("resolve", role))
        return self._registry.resolve(role)

    def ensure(self, identity):
        self.calls.append(("ensure", identity.role))
        return self._registry.ensure(identity)

    def receipt(self, identity, serving):
        self.calls.append(("receipt", identity.role))
        return self._registry.receipt(identity, serving)

    def roles(self, method: str) -> set[str]:
        return {role for called, role in self.calls if called == method}


@pytest.fixture
def world(tmp_path):
    """One seat under test, one bystander that must never be reached for."""
    write_snapshot(tmp_path / "remote", dict(FILES))
    pin = pin_snapshot(tmp_path / "remote", tmp_path / "manifests" / "seat.json")
    chairs = {
        "attestator_1": hf_chair("attestator_1", pin, manifest="manifests/seat.json"),
        BYSTANDER: hf_chair(BYSTANDER, pin, manifest="manifests/seat.json"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    registry = registry_for(config_of(tmp_path, chairs, witness_floor=2), tmp_path, fetcher)
    return Traced(registry), fetcher


def _assert_no_other_chair_was_reached_for(traced: Traced, fetcher: RecordingFetcher, chair: str):
    """The whole of "no other configured seat is invoked", in three assertions."""
    assert traced.roles("ensure") <= {chair}
    assert traced.roles("receipt") <= {chair}
    assert set(fetcher.roles) <= {chair}


# --- 1. A seat whose digest does not verify -----------------------------------------


def test_a_digest_that_does_not_verify_refuses_without_reaching_for_another_chair(world):
    traced, fetcher = world
    fetcher.files["weights.bin"] = b"something else entirely\n"
    identity = traced.resolve("attestator_1")

    with pytest.raises(DigestMismatchRefusal) as caught:
        traced.ensure(identity)

    assert caught.value.chair == "attestator_1"
    assert "weights.bin" in str(caught.value)
    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")
    assert BYSTANDER not in traced.roles("resolve")


# --- 2. A seat that will not resolve --------------------------------------------------


def test_an_unconfigured_role_refuses_rather_than_returning_a_neighbouring_chair(world):
    traced, fetcher = world

    with pytest.raises(UnresolvedChairRefusal) as caught:
        traced.resolve("perlector")

    assert caught.value.chair == "perlector"
    _assert_no_other_chair_was_reached_for(traced, fetcher, "perlector")
    assert traced.roles("resolve") == {"perlector"}


def test_a_neighbouring_revision_of_the_right_chair_is_still_refused(world):
    """The subtler shape of the same door: the role is right and the pin is not.
    Nothing may treat "close to the pin" as "the pin"."""
    traced, fetcher = world
    identity = traced.resolve("attestator_1")
    neighbouring = ChairIdentity(**{**identity.to_record(), "revision": "b" * 40})

    with pytest.raises(UnresolvedChairRefusal, match="neighbouring revision"):
        traced.ensure(neighbouring)

    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")


def test_a_chair_whose_snapshot_cannot_be_fetched_at_all_refuses(world):
    traced, fetcher = world
    fetcher.fail = RuntimeError("the pinned snapshot is not there")
    identity = traced.resolve("attestator_1")

    with pytest.raises(UnresolvedChairRefusal) as caught:
        traced.ensure(identity)

    assert caught.value.chair == "attestator_1"
    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")


# --- 3. A cache holding a different revision than the pin ----------------------------


def test_a_cache_describing_a_different_pin_refuses_and_fetches_nothing(world):
    """Harvest #43. The cache is never authoritative over the pin, and the pin is
    never quietly updated to whatever the cache turned out to hold."""
    traced, fetcher = world
    identity = traced.resolve("attestator_1")
    snapshot = traced.ensure(identity)
    descriptor = snapshot.root / CACHE_DESCRIPTOR
    record = json.loads(descriptor.read_text(encoding="utf-8"))
    record["revision"] = "b" * 40
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    fetcher.calls.clear()

    with pytest.raises(CacheRevisionRefusal) as caught:
        traced.ensure(identity)

    assert caught.value.chair == "attestator_1"
    assert fetcher.calls == [], "a mismatched cache is refused, never re-fetched over"
    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")


def test_a_cache_with_no_readable_descriptor_at_all_is_refused(world):
    traced, fetcher = world
    identity = traced.resolve("attestator_1")
    snapshot = traced.ensure(identity)
    (snapshot.root / CACHE_DESCRIPTOR).write_text("not json", encoding="utf-8")

    with pytest.raises(CacheRevisionRefusal, match="descriptor"):
        traced.ensure(identity)


# --- 4. An adapter that will not fetch ------------------------------------------------


@pytest.fixture
def adapter_world(tmp_path):
    """An adapter seat, its configured base, and a bystander witness."""
    write_snapshot(tmp_path / "remote", dict(FILES))
    pin = pin_snapshot(tmp_path / "remote", tmp_path / "manifests" / "seat.json")
    chairs = {
        "attestator_1": hf_chair(
            "attestator_1", pin, manifest="manifests/seat.json", adapter_of="base"
        ),
        "base": hf_chair("base", pin, manifest="manifests/seat.json", revision="b" * 40),
        BYSTANDER: hf_chair(BYSTANDER, pin, manifest="manifests/seat.json"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    registry = registry_for(config_of(tmp_path, chairs, witness_floor=2), tmp_path, fetcher)
    return Traced(registry), fetcher


def test_an_adapter_that_will_not_fetch_is_never_served_as_its_bare_base(adapter_world):
    """The failure mode this refusal exists for: the base fetches perfectly well,
    and serving it under the adapter's seat name would look like success."""
    traced, fetcher = adapter_world
    fetcher.fail = RuntimeError("adapter weights would not download")
    adapter = traced.resolve("attestator_1")

    with pytest.raises(AdapterFetchRefusal) as caught:
        traced.ensure(adapter)

    assert caught.value.chair == "attestator_1"
    assert fetcher.roles == ["attestator_1"], "the base's weights were never fetched instead"
    assert traced.roles("ensure") == {"attestator_1"}
    assert traced.roles("receipt") == set()


def test_an_adapter_cache_is_bound_to_its_base_pin_and_refuses_when_the_base_moves(
    adapter_world, tmp_path
):
    """An adapter's own pin is not enough. Its cache is only compatible with the
    base it was fetched against, so repinning the base must invalidate it — the
    alternative is an old adapter quietly riding a model it never saw.

    Resolving the named base to write that binding is the one place another
    role's *configuration* is read. It is read and nothing else: never fetched,
    never verified, never served, never offered as a substitute.
    """
    traced, fetcher = adapter_world
    adapter = traced.resolve("attestator_1")
    traced.ensure(adapter)
    fetched_once = list(fetcher.calls)

    base = traced.resolve("base")
    repinned = ChairRegistry(
        config_of(
            tmp_path,
            {
                "attestator_1": hf_chair(
                    "attestator_1",
                    adapter.digest_manifest,
                    manifest="manifests/seat.json",
                    adapter_of="base",
                ),
                "base": hf_chair(
                    "base", base.digest_manifest, manifest="manifests/seat.json", revision="c" * 40
                ),
                BYSTANDER: hf_chair(
                    BYSTANDER, base.digest_manifest, manifest="manifests/seat.json"
                ),
            },
            witness_floor=2,
        ),
        manifest_root=tmp_path,
        cache_root=traced.cache_root,
        fetcher=fetcher,
    )

    with pytest.raises(CacheRevisionRefusal) as caught:
        repinned.ensure(repinned.resolve("attestator_1"))

    assert caught.value.chair == "attestator_1"
    assert fetcher.calls == fetched_once, "nothing was fetched for the base or anyone else"


def test_an_adapter_whose_base_became_absent_is_refused(adapter_world, tmp_path):
    traced, fetcher = adapter_world
    adapter = traced.resolve("attestator_1")
    traced.ensure(adapter)

    # Built directly rather than through the parser, which refuses this pairing
    # outright: the question here is what `ensure` does if it ever sees one.
    from common.seats.models import AbsentChair, ModelsConfig

    hobbled = ModelsConfig(
        witness_floor=2,
        chairs={
            "attestator_1": adapter,
            "base": AbsentChair("base", "withdrawn between runs"),
            BYSTANDER: traced.resolve(BYSTANDER),
        },
        source_path=tmp_path / "models.toml",
    )
    registry = ChairRegistry(
        hobbled, manifest_root=tmp_path, cache_root=traced.cache_root, fetcher=fetcher
    )

    with pytest.raises(CacheRevisionRefusal, match="explicitly absent"):
        registry.ensure(registry.resolve("attestator_1"))


# --- 5. A serving recipe that will not start ------------------------------------------


def test_a_recipe_that_will_not_start_refuses_without_trying_a_second_route(world):
    """Starting is spec 04's business; this only refuses to let a failed start
    look like a receipt for something that ran, or become a second attempt under
    the same role name."""
    traced, fetcher = world
    identity = traced.resolve("attestator_1")

    with pytest.raises(ServingRecipeRefusal) as caught:
        traced.refuse_recipe_start(identity, "the engine process exited before it listened")

    assert caught.value.chair == "attestator_1"
    assert "exited before it listened" in str(caught.value)
    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")


def test_a_recipe_failure_for_a_chair_that_is_no_longer_the_configured_pin_is_refused(world):
    traced, fetcher = world
    identity = traced.resolve("attestator_1")
    neighbouring = ChairIdentity(**{**identity.to_record(), "serving_recipe": "some-other-recipe"})

    with pytest.raises(UnresolvedChairRefusal):
        traced.refuse_recipe_start(neighbouring, "irrelevant")


# --- 6. A seat configured absent -------------------------------------------------------


def test_an_explicit_absence_is_never_filled_by_a_configured_neighbour(tmp_path):
    chairs = {
        "attestator_1": hf_chair("attestator_1", "d" * 64),
        BYSTANDER: absent_chair("withdrawn for alpha"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    registry = registry_for(config_of(tmp_path, chairs, witness_floor=2), tmp_path, fetcher)
    traced = Traced(registry)

    absence = traced.resolve(BYSTANDER)

    assert absence.reason == "withdrawn for alpha"
    assert fetcher.calls == []
    assert traced.roles("ensure") == set()


# --- 7. A local path that escapes its model root ----------------------------------------


def test_a_local_path_escaping_the_model_root_refuses_without_touching_the_network(tmp_path):
    model_root = tmp_path / "model-fixtures"
    model_root.mkdir()
    outside = write_snapshot(tmp_path / "outside", {"weights.bin": b"somewhere else\n"})
    (model_root / "escape").symlink_to(outside, target_is_directory=True)
    write_snapshot(model_root / BYSTANDER, dict(FILES))
    pin = pin_snapshot(outside, tmp_path / "manifests" / "seat.json")

    chairs = {
        "perlector": local_chair("perlector", pin, path="escape", manifest="manifests/seat.json"),
        BYSTANDER: local_chair(BYSTANDER, pin, manifest="manifests/seat.json"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    registry = ChairRegistry(
        config_of(tmp_path, chairs, witness_floor=1, model_root="model-fixtures"),
        manifest_root=tmp_path,
        fetcher=fetcher,
    )
    traced = Traced(registry)

    with pytest.raises(LocalPathRefusal) as caught:
        traced.ensure(traced.resolve("perlector"))

    assert caught.value.chair == "perlector"
    _assert_no_other_chair_was_reached_for(traced, fetcher, "perlector")


# --- The taxonomy is closed --------------------------------------------------------------


def test_every_refusal_this_package_raises_is_a_member_of_the_closed_taxonomy():
    """A new failure mode has to be spelled as one of these, so a reader of the
    seven doors above can be sure the list is the whole list."""
    from common.seats.errors import ALL_REFUSAL_TYPES, is_closed_refusal

    for refusal in ALL_REFUSAL_TYPES:
        error = refusal("attestator_1", "a concrete difference")
        assert is_closed_refusal(error)
        assert error.chair == "attestator_1"
        assert "attestator_1" in str(error)
        # Every one is a ContractError, so a stage that hits one exits with the
        # honest fatal code rather than an unclassified traceback.
        from common.contracts.errors import ContractError

        assert isinstance(error, ContractError)

    assert not is_closed_refusal(ChairRefusal("attestator_1", "the base class is not a member"))
    assert not is_closed_refusal(ValueError("not a seat refusal at all"))


def test_a_refusal_carries_the_concrete_difference_and_not_only_the_chair(world):
    """ "seat 'attestator_1': it did not work" would satisfy "names the seat" and
    tell an operator nothing about what to change."""
    traced, fetcher = world
    fetcher.files["weights.bin"] = b"drifted\n"

    with pytest.raises(ChairRefusal) as caught:
        traced.ensure(traced.resolve("attestator_1"))

    difference = caught.value.difference
    assert "weights.bin" in difference
    assert difference.strip() not in ("", "attestator_1")
