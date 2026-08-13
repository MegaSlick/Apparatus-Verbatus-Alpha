"""The reader protocol: what actually looks at a dossier and reports a Lectio.

A real serving manager (spec 04, behind vLLM, live-pod only) is a future
implementation of this protocol. This chamber has no pod and no GPU, so the
only implementation here is `FixtureReader`, and the protocol is the seam that
lets a real reader replace it without `run.py`'s orchestration changing at all.

`primed` distinguishes the establishing pass (all testimonia in the dossier)
from Lectio nuda (none) -- one call shape, so a real reader has no second code
path to keep in sync. **This fixture reader cannot produce a genuinely
different unprimed reading**: there is no model behind it to diverge, so what
this build proves is the wiring and the module boundary keeping nuda out of the
Archetypus's reach, never the witness-dependence signal itself.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class LectioResult(TypedDict):
    text: str
    stop_reason: str | None


class Reader(Protocol):
    def read(self, dossier: dict[str, Any], *, primed: bool) -> LectioResult:
        """Produce one Lectio over one dossier."""


class FixtureReader:
    """Reads the declared fixture text and stop-reason for one act. Proves
    wiring, not reading -- the skeleton's stated and repeated limit."""

    def __init__(self, fixture: dict[str, Any], scenario: str):
        self._fixture = fixture
        self._scenario = scenario

    def read(self, dossier: dict[str, Any], *, primed: bool) -> LectioResult:
        act_key = dossier["act_key"]
        return {
            "text": self._reading_text(act_key),
            "stop_reason": self._declared_stop_reason(act_key),
        }

    def _reading_text(self, act_key: str) -> str:
        for act in self._fixture["act"]:
            if act["key"] == act_key:
                return act["text"]
        raise KeyError(f"the fixture declares no act {act_key!r}")

    def _declared_stop_reason(self, act_key: str) -> str | None:
        """The engine's own word on why it stopped.

        A reader always reports one, because a real serving engine always
        answers something -- the fixture table only declares the *unusual*
        answer, and every act it does not name stopped normally. Returning
        `None` here instead would say "this engine reported nothing", which is
        a different fact: `truncation.classify` holds on it rather than
        calling the reading complete, and nothing in this offline chamber is
        entitled to claim an engine went silent.
        """
        declared_scenarios = {scenario["name"] for scenario in self._fixture["scenario"]}
        declared_acts = {act["key"] for act in self._fixture["act"]}
        rows = self._fixture.get("stop_reason", [])
        # The whole table validates before any row is selected: a misspelt row
        # sitting after the match would otherwise stay unnoticed, silently
        # defaulting every act it meant to name to "stop" — a declared `length`
        # would vanish and a truncated reading would publish as complete.
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["scenario"], row["act_key"])
            if key in seen:
                raise KeyError(
                    f"stop_reason declares {key!r} twice; two contradictory rows would "
                    "publish whichever is written first and discard the other silently"
                )
            seen.add(key)
            if row["scenario"] not in declared_scenarios:
                raise KeyError(f"stop_reason row names undeclared scenario {row['scenario']!r}")
            if row["act_key"] not in declared_acts:
                raise KeyError(f"stop_reason row names undeclared act {row['act_key']!r}")
            if row["stop_reason"] not in {"stop", "length"}:
                raise KeyError(f"stop_reason row declares unknown signal {row['stop_reason']!r}")
        for row in rows:
            if row["scenario"] == self._scenario and row["act_key"] == act_key:
                return row["stop_reason"]
        return "stop"
