"""Perlector: reads each sealed page whole, with the testimonia as fallible clues.

The pass reads every sealed Exemplar page once (`page_run.py`) and seals the
stage. This module opens the pass: its sealed configs, its chair, its serving
mode and its concurrency. What a live call records, and how a resumed pass
reads it back, is `live_calls.py`.

The sealed serving-recipe row picks the reader. A `kind = "vllm"` row for the
Perlector chair reads live; any other row reads the fixture's declared page
answers, which prove wiring only. A real submission has no fixture
declaration, so a non-live row there refuses before anything is published
(`common.stage.refuse_unlive_real_reading`).

    python pipeline/4_perlector/run.py --run-root <dir> --run-id <id>
"""

import sys
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import page_run  # noqa: E402
import protocol  # noqa: E402

from common.alignment import load_dissent_limits  # noqa: E402
from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import PERLECTOR  # noqa: E402
from common.decoding import load_decoding_policy, perlector_page_max_tokens  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    PERLECTOR_CHAIR,
    open_stage_context,
    refuse_unlive_real_reading,
    run_stage,
    stage_parser,
)
from operations.serving.assembly import (  # noqa: E402
    bound_serving_recipes,
    stage_chair_client,
)
from operations.serving.client import ChairClient, serving_mode_for  # noqa: E402
from operations.serving.errors import ChairResponseRefusal  # noqa: E402

DESCRIPTION = "Perlector: reads each sealed page whole, with the testimonia as fallible clues."


def perlector_chair(context) -> ChairIdentity | AbsentChair:
    """The Perlector chair, resolved by name. Never another chair, never a base."""
    resolved = context.registry.resolve(PERLECTOR_CHAIR)
    if not isinstance(resolved, (ChairIdentity, AbsentChair)):
        raise ContractError("Perlector resolution returned neither an identity nor an absence")
    return resolved


def perlector_serving_mode(context, args, chair: ChairIdentity | AbsentChair) -> str:
    """`"fixture"` or `"live"`, from the sealed serving-recipe row kind alone.

    The catalogue is already sealed into `config_digest`; `--placement-tier` is a
    measured fact of the card and deliberately unsealed. Resolved before anything is
    published or started, so a live row without a tier refuses on an untouched tree. An
    absent chair is `fixture`: it reads nothing and has no identity to look a row up by.
    """
    if isinstance(chair, AbsentChair):
        return "fixture"
    return serving_mode_for(
        bound_serving_recipes(context, args.serving_recipes_config), chair, args.placement_tier
    )


class ResidentChair:
    """The one live chair a Perlector pass holds, and the promise it is stopped.

    `main` closes it in a `finally`, and the pass closes it before sealing, so a failed
    shutdown is never reported over a sealed stage; `close` is idempotent for that
    reason. A `ServiceStopError` propagates: an unverified shutdown must be reported.
    """

    __slots__ = ("client", "starting")

    def __init__(self) -> None:
        self.client: ChairClient | None = None
        # A start running on a background thread (`live_calls.BackgroundStart`), if any.
        self.starting = None

    def close(self) -> None:
        """Stop the chair; a chair still starting is left to its start's thread, which
        stops it when the start returns, so a stopped pass does not wait for a load."""
        starting, self.starting = self.starting, None
        if starting is not None and starting.abandon():
            return
        client, self.client = self.client, None
        if client is not None:
            client.__exit__()


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run the pass, and guarantee any chair it started is stopped.

    Both parameters are test seams; neither decides which engine answers, which is the
    sealed serving-recipe row's business. `_read_the_pages` stops the chair before
    sealing; the `finally` covers a pass that raised first.

    `ChairResponseRefusal` is a `RuntimeError`, which `run_stage` does not catch, so it
    is re-raised as a `ContractError` to exit as a named refusal. `ChairRequestRefusal`
    is deliberately not caught: it means this code built a bad request, and a traceback
    at the construction site is worth more.
    """
    service = ResidentChair()
    try:
        return _read_the_pages(registry_factory, serving_factory, service)
    except ChairResponseRefusal as refusal:
        raise ContractError(f"{type(refusal).__name__}: {refusal}") from refusal
    finally:
        service.close()


def _read_the_pages(registry_factory, serving_factory, service: ResidentChair) -> int:
    """One Perlector pass: every sealed page read whole (`page_run.py`), then the seal."""
    run = _open_pass(registry_factory, serving_factory, service)
    page_run.read_the_pages(run)
    # Before the seal, so a failed shutdown is never reported over a sealed stage;
    # `close` is idempotent with `main`'s `finally`.
    service.close()
    run.context.seal_boundary()
    run.context.finish()
    return EXIT_COMPLETE


@dataclass
class _Pass:
    """The sealed inputs one Perlector pass reads every page under, and its live chair."""

    context: Any
    args: Any
    service: ResidentChair
    client_factory: Any
    chair: ChairIdentity | AbsentChair
    serving_mode: str
    protocol_config: dict[str, Any]
    # The sealed decoding policy, and from it the output cap of one whole-page reading.
    decoding_policy: dict[str, Any]
    page_max_tokens: int
    audit_policy: dict[str, Any]
    audit_sha256: str
    # The sealed step budget of every dissent comparison.
    dissent_steps: int
    receipt_ref: dict[str, str] | None = None
    # Reader calls in flight at once; see `_reading_concurrency`.
    concurrency: int = 1


def _open_pass(registry_factory, serving_factory, service: ResidentChair) -> _Pass:
    """Resolve every sealed input the pass needs, refusing before anything is published."""
    parser = stage_parser(DESCRIPTION)
    parser.add_argument(
        "--reading-deadline",
        type=_utc,
        default=None,
        help="UTC time by which a live pass must finish reading; it refuses to start, or "
        "to read another page, when the planned calls would run past it",
    )
    parser.add_argument(
        "--perlector-concurrency",
        type=_positive_int,
        default=None,
        help="reader calls a live pass keeps in flight at once, so the engine can batch "
        "them; capped by the served row's max_num_seqs, which is also the default. "
        "A fixture pass reads one page at a time",
    )
    args = parser.parse_args()
    context = open_stage_context(args, PERLECTOR, registry_factory=registry_factory)
    decoding_policy, decoding_sha256 = load_decoding_policy(args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    chair = perlector_chair(context)
    serving_mode = perlector_serving_mode(context, args, chair)
    refuse_unlive_real_reading(context, chair, serving_mode)
    protocol_config, protocol_sha256 = protocol.load(context.perlector_protocol_config_path)
    context.require_sealed_config("perlector-protocol", protocol_sha256)
    audit_policy, audit_sha256 = audit.load(context.perlector_audit_config_path)
    context.require_sealed_config("perlector-audit", audit_sha256)
    dissent_limits, alignment_sha256 = load_dissent_limits(args.alignment_config)
    context.require_sealed_config("alignment", alignment_sha256)
    return _Pass(
        context=context,
        args=args,
        service=service,
        client_factory=serving_factory
        or partial(
            stage_chair_client,
            decoding_policy=decoding_policy,
            decoding_config_sha256=decoding_sha256,
        ),
        chair=chair,
        serving_mode=serving_mode,
        protocol_config=protocol_config,
        decoding_policy=decoding_policy,
        page_max_tokens=perlector_page_max_tokens(decoding_policy),
        audit_policy=audit_policy,
        audit_sha256=audit_sha256,
        dissent_steps=dissent_limits.max_comparison_steps,
        concurrency=_reading_concurrency(context, args, chair, serving_mode),
    )


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} names no time zone")
    return parsed


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise ValueError(f"{value!r} is not a positive count")
    return number


def _reading_concurrency(context, args, chair, serving_mode: str) -> int:
    """How many reader calls may be in flight at once.

    Only a live engine batches, and never beyond its served row's `max_num_seqs`.
    """
    if serving_mode != "live":
        return 1
    bound = (
        bound_serving_recipes(context, args.serving_recipes_config)
        .for_identity(chair, args.placement_tier)
        .max_num_seqs
    )
    return min(args.perlector_concurrency or bound, bound)


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
