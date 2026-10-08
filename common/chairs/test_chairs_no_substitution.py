"""No substitution: the property this whole package exists for.

Every refusal, injected, raises with the chair named, and no other configured
chair is invoked while it is handled.

Seven doors lead to a refusal, and each is a refusal and never a substitution:

  1. a chair whose digest does not verify
  2. a chair that will not resolve — never a fake, never a base, never a
     neighbouring revision
  3. a cache holding a different revision than the pin (a pin is a constant the
     artifact must MATCH, never a value the artifact supplies)
  4. a chair declared as an adapter of another — refused when the roster is
     read (`test_chairs_resolution.py`), so no base can answer in its place
  5. a serving recipe that will not start — never a second route under the same
     role name
  6. a chair configured `absent`, which stays in the roster as an explicit
     absence rather than being quietly filled
  7. a local-repository path that escapes its model root

Every roster below carries at least one other configured chair, so "no other
configured chair is invoked" is a real claim rather than a vacuous one, and every
door is driven through the *real* registry. Two logs are asserted on: the fetch
seam's, and a trace of every `resolve`/`ensure`/`receipt` call the registry made
while handling the refusal.

No step picks, and that covers operations as well as models: nothing about a
picker requires that it be called one, or that a model be the thing doing the
choosing.
"""

import json

import pytest

from common.chairs.errors import (
    CacheRevisionRefusal,
    ChairRefusal,
    DigestMismatchRefusal,
    LocalPathRefusal,
    ServingRecipeRefusal,
    UnresolvedChairRefusal,
)
from common.chairs.models import ChairIdentity
from common.chairs.registry import CACHE_DESCRIPTOR, ChairRegistry

from .conftest import (
    RecordingFetcher,
    absent_chair,
    config_of,
    hf_chair,
    local_chair,
    pin_snapshot,
    write_snapshot,
)

BYSTANDER = "attestator_2"
FILES = {"weights.bin": b"expected fixture bytes\n"}
_DEFAULT = object()


class Traced(ChairRegistry):
    """Every protocol call the registry makes, in order, with the role asked for.

    A refusal that named the right chair could still have reached for another one
    on the way — resolving it, fetching it, receipting it — and the raised error
    would look identical. This is what makes the difference visible.

    **A subclass, not a wrapper.** It overrides `resolve`, `ensure` and `receipt`
    so the registry's own internal calls, such as `_require_current_identity`, are
    logged too.
    Python binds `self.resolve` on the instance, so the log sits inside the
    registry rather than in front of it, and a substitution introduced inside
    `ensure()` or `receipt()` shows up in it. A delegating wrapper would see only
    the calls the test made through it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, str]] = []

    def resolve(self, role: str):
        self.calls.append(("resolve", role))
        return super().resolve(role)

    def ensure(self, identity):
        self.calls.append(("ensure", identity.role))
        return super().ensure(identity)

    def receipt(self, identity, serving):
        self.calls.append(("receipt", identity.role))
        return super().receipt(identity, serving)

    def roles(self, method: str) -> set[str]:
        return {role for called, role in self.calls if called == method}


def traced_for(config, tmp_path, fetcher, *, cache_root=_DEFAULT) -> Traced:
    """`conftest.registry_for`, but building the tracing registry itself."""
    return Traced(
        config,
        manifest_root=tmp_path,
        cache_root=tmp_path / "cache" if cache_root is _DEFAULT else cache_root,
        fetcher=fetcher,
    )


@pytest.fixture
def world(tmp_path):
    """One chair under test, one bystander that must never be reached for."""
    write_snapshot(tmp_path / "remote", dict(FILES))
    pin = pin_snapshot(tmp_path / "remote", tmp_path / "manifests" / "chair.json")
    chairs = {
        "attestator_1": hf_chair("attestator_1", pin, manifest="manifests/chair.json"),
        BYSTANDER: hf_chair(BYSTANDER, pin, manifest="manifests/chair.json"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    return traced_for(config_of(tmp_path, chairs, witness_floor=2), tmp_path, fetcher), fetcher


def _assert_no_other_chair_was_reached_for(traced: Traced, fetcher: RecordingFetcher, chair: str):
    """The whole of "no other configured chair is invoked", in four assertions.

    `resolve` matters most: a substitution reaches for its replacement by
    resolving it, and every internal resolution the registry makes runs through
    the override above.
    """
    assert traced.roles("resolve") <= {chair}
    assert traced.roles("ensure") <= {chair}
    assert traced.roles("receipt") <= {chair}
    assert set(fetcher.roles) <= {chair}


# --- 1. A chair whose digest does not verify -----------------------------------------


def test_a_digest_that_does_not_verify_refuses_without_reaching_for_another_chair(world):
    traced, fetcher = world
    fetcher.files["weights.bin"] = b"something else entirely\n"
    identity = traced.resolve("attestator_1")

    with pytest.raises(DigestMismatchRefusal) as caught:
        traced.ensure(identity)

    assert caught.value.chair == "attestator_1"
    assert "weights.bin" in str(caught.value)
    _assert_no_other_chair_was_reached_for(traced, fetcher, "attestator_1")


# --- 2. A chair that will not resolve --------------------------------------------------


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
    """The cache is never authoritative over the pin, and the pin is
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


def test_a_role_never_names_a_cache_directory(world, tmp_path):
    """The cache is keyed by the pinned manifest digest, so even a role the parser
    would refuse, such as a leading dot, cannot reach `.`, `..` or the registry's
    own hidden work directories."""
    traced, fetcher = world

    # Built directly rather than through the parser, which refuses this role
    # outright: the question here is what `ensure` does if it ever sees one.
    from dataclasses import replace

    from common.chairs.models import ModelsConfig

    hidden = replace(traced.resolve("attestator_1"), role=".hidden")
    config = ModelsConfig(
        witness_floor=0,
        chairs={".hidden": hidden},
        source_path=tmp_path / "models.toml",
    )
    registry = ChairRegistry(
        config, manifest_root=tmp_path, cache_root=traced.cache_root, fetcher=fetcher
    )

    snapshot = registry.ensure(registry.resolve(".hidden"))

    assert snapshot.root == traced.cache_root / "by-digest" / hidden.digest_manifest
    assert snapshot.identity.role == ".hidden"
    assert not (traced.cache_root / ".hidden").exists()


def test_an_unsafe_digest_is_refused_before_it_names_a_cache_directory(world, tmp_path):
    traced, fetcher = world
    from dataclasses import replace

    unsafe = replace(traced.resolve("attestator_1"), digest_manifest="../escape")

    with pytest.raises(CacheRevisionRefusal, match="unsafe as a cache path"):
        traced._ensure_huggingface(unsafe, traced.manifest(traced.resolve("attestator_1")))
    assert fetcher.calls == []
    assert not (tmp_path / "escape").exists()


# --- 5. A serving recipe that will not start ------------------------------------------


def test_a_recipe_that_will_not_start_refuses_without_trying_a_second_route(world):
    """Starting belongs to the serving manager; this only refuses to let a failed start
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


# --- 6. A chair configured absent -------------------------------------------------------


def test_an_explicit_absence_is_never_filled_by_a_configured_neighbour(tmp_path):
    chairs = {
        "attestator_1": hf_chair("attestator_1", "d" * 64),
        BYSTANDER: absent_chair("withdrawn for alpha"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    traced = traced_for(config_of(tmp_path, chairs, witness_floor=2), tmp_path, fetcher)

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
    pin = pin_snapshot(outside, tmp_path / "manifests" / "chair.json")

    chairs = {
        "perlector": local_chair("perlector", pin, path="escape", manifest="manifests/chair.json"),
        BYSTANDER: local_chair(BYSTANDER, pin, manifest="manifests/chair.json"),
    }
    fetcher = RecordingFetcher(dict(FILES))
    traced = traced_for(
        config_of(tmp_path, chairs, witness_floor=1, model_root="model-fixtures"),
        tmp_path,
        fetcher,
        cache_root=None,
    )

    with pytest.raises(LocalPathRefusal) as caught:
        traced.ensure(traced.resolve("perlector"))

    assert caught.value.chair == "perlector"
    _assert_no_other_chair_was_reached_for(traced, fetcher, "perlector")


# --- The taxonomy is closed --------------------------------------------------------------


def test_every_raise_in_this_package_names_a_member_of_the_closed_taxonomy():
    """A new failure mode raised by the package has to be spelled as one of these.

    Every `raise` with an expression in the package's non-test modules is read
    from source and must construct a named member of `ALL_REFUSAL_TYPES`. A bare
    `raise` passes unread: this proves the raise sites, not the type of every
    exception that can escape through one. `conftest.py` is fixture code and is
    left out: its deterministic registry raises `RuntimeError` when constructed
    outside a pytest session, and its fetcher raises whatever failure a test
    injects."""
    import ast
    from pathlib import Path

    from common.chairs.errors import ALL_REFUSAL_TYPES

    allowed = {refusal.__name__ for refusal in ALL_REFUSAL_TYPES}
    package = Path(__file__).resolve().parent
    modules = [
        path
        for path in sorted(package.glob("*.py"))
        if not path.name.startswith("test_") and path.name != "conftest.py"
    ]
    constructed: list[str] = []
    outside: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc
            name = (
                exc.func.id
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name)
                else None
            )
            if name in allowed:
                constructed.append(name)
            else:
                outside.append(f"{path.name}:{node.lineno}: {ast.unparse(exc)}")

    assert constructed, "no refusal construction was found; the scan read nothing"
    assert outside == []


def test_the_closed_taxonomy_is_every_refusal_errors_py_declares():
    """`ALL_REFUSAL_TYPES` is the whole of what `errors.py` defines, and every
    member names the chair and is a `ContractError`."""
    import common.chairs.errors as errors_module
    from common.chairs.errors import ALL_REFUSAL_TYPES

    declared = {
        value
        for value in vars(errors_module).values()
        if isinstance(value, type)
        and issubclass(value, errors_module.ChairRefusal)
        and value is not errors_module.ChairRefusal
    }
    assert declared == set(ALL_REFUSAL_TYPES)

    for refusal in ALL_REFUSAL_TYPES:
        error = refusal("attestator_1", "a concrete difference")
        assert error.chair == "attestator_1"
        assert "attestator_1" in str(error)
        # Every one is a ContractError, so a stage that hits one exits with the
        # honest fatal code rather than an unclassified traceback.
        from common.contracts.errors import ContractError

        assert isinstance(error, ContractError)


def test_a_refusal_carries_the_concrete_difference_and_not_only_the_chair(world):
    """ "chair 'attestator_1': it did not work" would satisfy "names the chair" and
    tell an operator nothing about what to change."""
    traced, fetcher = world
    fetcher.files["weights.bin"] = b"drifted\n"

    with pytest.raises(ChairRefusal) as caught:
        traced.ensure(traced.resolve("attestator_1"))

    difference = caught.value.difference
    assert "weights.bin" in difference
    assert difference.strip() not in ("", "attestator_1")


def test_filling_a_chair_keeps_foreign_dirs_and_evicts_its_abandoned_work_dirs(tmp_path):
    write_snapshot(tmp_path / "remote", dict(FILES))
    pin = pin_snapshot(tmp_path / "remote", tmp_path / "manifests" / "chair.json")
    other_files = {"weights.bin": b"the base chair's own bytes\n"}
    write_snapshot(tmp_path / "remote-base", other_files)
    base_pin = pin_snapshot(tmp_path / "remote-base", tmp_path / "manifests" / "base.json")
    chairs = {
        "attestator_1": hf_chair("attestator_1", pin, manifest="manifests/chair.json"),
        "base": hf_chair("base", base_pin, manifest="manifests/base.json"),
    }
    traced = traced_for(config_of(tmp_path, chairs), tmp_path, RecordingFetcher(dict(FILES)))
    traced.ensure(traced.resolve("attestator_1"))
    cache = tmp_path / "cache" / "by-digest"
    (cache / f".{base_pin}.candidate-abandoned").mkdir()
    orphaned_backup = cache / f".{pin}.prior-4242"
    orphaned_backup.mkdir()
    (orphaned_backup / "weights").write_bytes(b"a promote that died mid-swap")
    foreign = cache / "no-longer-configured"
    foreign.mkdir()
    (foreign / "user-data").write_text("keep", encoding="utf-8")
    beside = tmp_path / "cache" / "beside"
    beside.mkdir()

    traced.fetcher = RecordingFetcher(other_files)
    traced.ensure(traced.resolve("base"))

    assert (cache / base_pin / CACHE_DESCRIPTOR).is_file()
    assert (cache / pin / CACHE_DESCRIPTOR).is_file()
    assert not (cache / f".{base_pin}.candidate-abandoned").exists()
    assert not orphaned_backup.exists()
    assert (foreign / "user-data").read_text(encoding="utf-8") == "keep"
    assert beside.is_dir()
