"""RecordGold corpus: row snapshot, fetch plan, hold-out ledger, fetcher,
submission builder, reference truth, comparator, local admission, and evaluation.

RecordGold (`Teklia/DAI-CReTDHI-RecordGold-ATR`) is a third-party expert-annotated
corpus this project's real roster names two of its own chairs against
(`config/models-real.toml`'s `attestator_2` and `secondary_proposer`); the
fixture roster binds both to fixture snapshots, not the DAI weights, and `README.md`'s "The DAI contamination risk"
section states what is and is not known about their training data.
`rows.py` reads the three parquets' facts (converted once, offline, to a
self-hashed JSON snapshot outside this package) and seals them; `plan.py`
derives, from that snapshot alone, which IIIF pages exist and how their
records group; `holdout.py` names which pages the `test` split protects;
`fetch.py` and `cache.py` fetch and cache page bytes politely and resumably;
`integrate.py` turns a sealed fetch log into the `FetchedPage` objects
`submission.py` takes; `submission.py` and `sidecar.py` build a Door-shaped
submission from cached bytes; `reference.py` mints reference-truth records
from RecordGold's annotations; `compare.py` scores a sealed pipeline run
against that reference truth; `local_admission.py` admits the RecordGold sets
already on this machine as reference truth, every record admitted or refused by
name; `evaluate.py` is the one caller that builds `compare.py`'s hypotheses from
a real run's sealed Armarium export and writes the evaluation record;
`witness_evaluate.py` scores each witness's sealed page Testimonia against that
reference; `exactly_once.py` checks a page-read run read every record exactly
once; `canary.py` is a private golden-canary alarm over a fetched run tree;
`perlector_request_fit.py` and `length_floor_calibration.py` measure a page
manifest against the served Perlector row and the truncation floor. See
`README.md` for each module's shape in full.

**Package rule**, binding every module in this package: `operations/corpus/`
may not import `pipeline/`, and `pipeline/` may not import `operations.corpus`
— the same one-way rule `operations/submit/` already carries, pinned by
`test_compare.py::test_no_pipeline_module_imports_operations_corpus`, which
walks `pipeline/` and fails on any import of this package.

**A delegated refusal travels under the calling module's own name.** When a
module in this package calls another and that call refuses, the caller wraps the
refusal under one of its own declared reasons and carries the original text in
the detail. A caller dispatching on a module's closed vocabulary must never meet
a name from a vocabulary it was not given -- which is exactly what a closed set
is for -- and the alternative reading, that a delegated module's refusal should
travel under its own name, would make every declared set open in practice.

**Not a picker.** Nothing in this package selects among readings
or witnesses. `plan.py` groups rows that already exist by the page they already
belong to; `holdout.py` names pages the `test` split protects; `compare.py`
runs only after a pipeline run is sealed, selects nothing about what the
pipeline read, and drops nothing from either side of a pairing. All of it
refuses; none of it chooses.
"""

from typing import Any

from common.contracts.errors import ContractError


class CorpusRefusal(ContractError):
    """Every refusal this package raises, as `"<reason>: <detail>"`.

    Each module raises its own `Refusal` subclass whose `reasons` is that module's closed
    `*_REFUSAL_REASONS` vocabulary. `.reason` is the leading name; a caller
    dispatches on it, never on the text. A name outside the vocabulary is a
    programming error, raised as `TypeError` at construction.
    """

    reasons: frozenset[str] = frozenset()

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = message.split(":", 1)[0]
        if self.reason not in self.reasons:
            raise TypeError(
                f"{type(self).__module__} declares no reason {self.reason!r}: {message}"
            )

    @classmethod
    def closed(cls, value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
        """`value` if it is a dict carrying exactly `fields`, else `malformed-record`."""
        if not isinstance(value, dict) or set(value) != fields:
            raise cls(f"malformed-record: {what} must be the closed record {sorted(fields)}")
        return value


__all__ = ["CorpusRefusal"]
