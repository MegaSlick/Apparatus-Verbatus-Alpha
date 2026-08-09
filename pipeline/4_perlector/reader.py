"""The reader protocol: what actually looks at a dossier and reports a Lectio.

A real serving manager (spec 04, behind vLLM, live-pod only) is a future
implementation of this protocol. This chamber has no pod and no GPU, so the
only implementation here is `FixtureReader`: it draws its answer from the
declared fixture exactly as `pipeline/4_perlector/run.py` already did inline
before this module existed, factored out so a real reader can occupy the same
seam later without touching `run.py`'s orchestration at all.

`primed` distinguishes the establishing pass (all testimonia in the dossier)
from Lectio nuda (none) -- one protocol, one call shape, never two code paths a
future real reader has to remember to keep in sync. This fixture reader cannot
produce a genuinely different unprimed reading (there is no model behind it to
diverge), which is named here rather than glossed over: what this build proves
is the wiring and the module boundary that keeps nuda out of the Archetypus's
reach, not the actual witness-dependence signal a real reader would produce.
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
        for row in self._fixture.get("stop_reason", []):
            if row["scenario"] == self._scenario and row["act_key"] == act_key:
                return row["stop_reason"]
        return None
