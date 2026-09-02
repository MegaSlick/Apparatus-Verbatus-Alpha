"""RecordGold corpus: row snapshot, fetch plan, hold-out ledger, fetcher,
submission builder, reference truth, and comparator.

RecordGold (`Teklia/DAI-CReTDHI-RecordGold-ATR`) is a third-party expert-annotated
corpus this project's drafted real roster names two of its own chairs against
(`config/models.toml`'s `attestator_2` and `secondary_proposer`); neither chair
is bound in the live config today, and `README.md`'s "The DAI contamination
risk" section states that as an unresolved inference, not a verified fact.
`rows.py` reads the three parquets' facts (converted once, offline, to a
self-hashed JSON snapshot outside this package) and seals them; `plan.py`
derives, from that snapshot alone, which IIIF pages exist and how their
records group; `holdout.py` names which pages the `test` split protects;
`fetch.py` and `cache.py` fetch and cache page bytes politely and resumably;
`integrate.py` turns a sealed fetch log into the `FetchedPage` objects
`submission.py` takes; `submission.py` and `sidecar.py` build a Door-shaped
submission from cached bytes; `reference.py` mints reference-truth records
from RecordGold's annotations; `compare.py` scores a sealed pipeline run
against that reference truth. See `SPEC.md` and `README.md` for each module's
shape in full.

**Package rule**, binding every module in this package: `operations/corpus/`
may not import `pipeline/`, and `pipeline/` may not import `operations.corpus`
— the same one-way rule `operations/submit/` already carries, pinned by
`test_compare.py::test_no_pipeline_module_imports_operations_corpus`, which
walks `pipeline/` and fails on any import of this package.

**Not a picker (hard rule 8).** Nothing in this package selects among readings
or witnesses. `plan.py` groups rows that already exist by the page they already
belong to; `holdout.py` names pages the `test` split protects; `compare.py`
runs only after a pipeline run is sealed, selects nothing about what the
pipeline read, and drops nothing from either side of a pairing. All of it
refuses; none of it chooses.
"""

from common.contracts.errors import ContractError


class CorpusRefusal(ContractError):
    """Every refusal this package raises, named at the front of its message.

    Convention: `"<reason-name>: <detail>"`, where `<reason-name>` is one of the
    closed `*_REFUSAL_REASONS` vocabulary each module in this package declares.
    A caller that wants to dispatch on the reason reads `str(error).split(":", 1)[0]`; a human
    reading the raised text sees the same name as its first word. This is what
    "refusals by name" means mechanically in this package: the name is not a
    label attached after the fact, it is the exception's own leading token.
    """


__all__ = ["CorpusRefusal"]
