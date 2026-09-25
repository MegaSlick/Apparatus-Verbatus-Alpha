"""Where this code expects to be run from, checked once before any work starts.

Verbatus runs from a source checkout, never an installed wheel: a built wheel
carries no `pipeline/`, `config/` or `proof/`, while stage code resolves its
defaults as siblings of those directories -- the checkout's shape, not an
installed package's. Refusing early states that in one sentence, before a run
begins, instead of meeting a missing-file error part way through.

`gold/` is deliberately not among the directories checked below: it is real
comparison material, not required to start, and the repair for a wheel that
lacks it is not to package private material into the wheel to make it import.
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
            "The pod bootstrap checks out a pinned commit in a checkout the pod image "
            "already carries and runs "
            "`uv sync --locked`; installing a built wheel is not a supported way to "
            "run this, because the wheel deliberately carries no configuration, "
            "stage code, or proof material. Run from a checkout of the repository."
        )
    return root
