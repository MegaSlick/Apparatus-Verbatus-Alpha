"""RecordGold bear corpus — Unit 1: row snapshot, fetch plan, hold-out ledger.

RecordGold (`Teklia/DAI-CReTDHI-RecordGold-ATR`) is a third-party expert-annotated
corpus this project trains two of its own chairs on (`config/models.toml`'s
`attestator_2` and `secondary_proposer`). This unit touches no network and no
image: it reads the three parquets' facts (converted once, offline, to a
self-hashed JSON snapshot outside this package) and derives, from that snapshot
alone, which IIIF pages exist, how their records group, and which pages the
`test` split holds out. Later units (fetch, submission, reference, comparator —
see `SPEC.md`) build on what this one measures.

**Package rule**, binding this unit and every later one: `operations/corpus/` may
not import `pipeline/`, and `pipeline/` may not import `operations.corpus` — the
same one-way rule `operations/submit/` already carries, pinned by an import-graph
test once a comparator exists to enforce it against (U4). This unit imports
nothing from `pipeline/`, so the rule is trivially kept here.

**Not a picker (hard rule 8).** Nothing in this unit selects among readings or
witnesses. `plan.py` groups rows that already exist by the page they already
belong to; `holdout.py` names pages the `test` split protects. Both refuse; neither
chooses.
"""

from common.contracts.errors import ContractError


class CorpusRefusal(ContractError):
    """Every refusal this package raises, named at the front of its message.

    Convention: `"<reason-name>: <detail>"`, where `<reason-name>` is one of this
    package's closed refusal vocabularies (`rows.ROW_REFUSAL_REASONS`,
    `plan.PLAN_REFUSAL_REASONS`, `holdout.HOLDOUT_REFUSAL_REASONS`). A caller that
    wants to dispatch on the reason reads `str(error).split(":", 1)[0]`; a human
    reading the raised text sees the same name as its first word. This is what
    "refusals by name" means mechanically in this package: the name is not a
    label attached after the fact, it is the exception's own leading token.
    """


__all__ = ["CorpusRefusal"]
