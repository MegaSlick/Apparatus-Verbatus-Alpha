"""RecordGold corpus: admit the local RecordGold sets as reference truth and score
sealed pipeline runs against them.

RecordGold (`Teklia/DAI-CReTDHI-RecordGold-ATR`) is a third-party expert-annotated
corpus. `README.md` lists each module and the proof run that uses them.

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

**Not a picker.** Nothing in this package selects among readings or witnesses.
`compare.py` runs only after a pipeline run is sealed, selects nothing about what
the pipeline read, and drops nothing from either side of a pairing.
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
