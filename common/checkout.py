"""Where this code expects to be run from, checked once before any work starts.

**Verbatus runs from a source checkout. There is no wheel installation, and this
module is the place that says so out loud.**

The evidence, from the tree rather than from intention.  `operations/pod/
bootstrap.py` puts the code on a pod by `git fetch` and `git checkout --detach`
at a pinned commit and then runs `uv sync --locked --group pod`; nothing builds,
ships, or installs a distribution.  The repository's own CI test asserts the
workflow does not `pip install .`.  And `pyproject.toml`'s discovery is
`include = ["common", "common.*", "operations", "operations.*"]` with namespaces
off, so a built wheel would carry no `pipeline/`, `config/`, `proof/` or `gold/`
at all — while `common/stage.py` and `operations/submit/gate.py` resolve their
defaults as siblings of those packages, which is the checkout's shape and not an
installed package's.

An outside review built that wheel and found what follows from it: installed
outside a checkout, loading the default data-handling policy failed on an absent
`config/data_handling_policy.json`.  The repair is *not* to package the private
material to make the import succeed.  It is to state the contract and refuse
early, so an operator who has somehow started outside a checkout is told that in
one sentence, before a run begins, instead of meeting a missing-file error part
way through.

The three directories named below are the ones runtime code reads by
checkout-relative path.  `gold/` is deliberately not among them: it is real
comparison material, it is not required to start, and demanding it would turn a
legitimate run into a refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]

CHECKOUT_RESOURCE_DIRECTORIES: Final = ("config", "pipeline", "proof")
"""Directories runtime code resolves relative to the checkout, not to a package.

`config/` holds every sealed stage configuration and the data-handling policy;
`pipeline/` holds the stage programs themselves; `proof/` holds the acceptance
material stages check against.  A wheel carries none of them.
"""


class NotACheckoutRefusal(RuntimeError):
    """Verbatus was started from something that is not a source checkout."""


def missing_checkout_resources(root: Path = REPOSITORY_ROOT) -> tuple[str, ...]:
    """Names of the checkout-relative directories this root does not have."""

    return tuple(name for name in CHECKOUT_RESOURCE_DIRECTORIES if not (root / name).is_dir())


def require_checkout(root: Path = REPOSITORY_ROOT) -> Path:
    """Refuse, in one plain sentence, before a run that could not finish begins."""

    missing = missing_checkout_resources(root)
    if missing:
        raise NotACheckoutRefusal(
            "Verbatus runs from a source checkout and this is not one: "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not beside "
            f"the installed code at {root}. "
            "The pod bootstrap clones the repository at a pinned commit and runs "
            "`uv sync --locked`; installing a built wheel is not a supported way to "
            "run this, because the wheel deliberately carries no configuration, "
            "stage code, or proof material. Run from a checkout of the repository."
        )
    return root
