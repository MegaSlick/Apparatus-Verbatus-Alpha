"""The reader protocol: what actually looks at a dossier and reports a Lectio.

A real serving manager (spec 04, behind vLLM, live-pod only) is a future
implementation of this protocol. This chamber has no pod and no GPU, so the
only implementation here is `FixtureReader`, and the protocol is the seam that
lets a real reader replace it without `run.py`'s orchestration changing at all.

`pass_kind` names every pass explicitly. A boolean could not distinguish the
production prior, the sampled control, nuda, and the production Perlectio.

**A real reader may not condition its generation on `pass_kind`.** It is
routing, not evidence: `lectio-nuda` and `lectio-prior` are built from
identical dossier arguments and therefore carry the same `dossier_digest` and
the same `rendered_sha256`, so `pass_kind` is the *only* thing that
distinguishes them at this seam. A reader that read it and behaved differently
would make the witness-dependence contrast the whole instrument exists for
measure the pipeline's own label instead of the model, which is GOVERNANCE 10's
"the instrument may not constrain what it measures" at the reader boundary.
`FixtureReader` reads it because it has no model behind it and must stand in
for one; that is the exception the docstring on `_declared_prior_reading`
names, not the pattern to copy.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, TypedDict

from common.contracts.errors import ContractError
from common.contracts.identities import act_id as derive_act_id
from common.imaging import grayscale_rows
from common.stage import FALLBACK_PAGE_ACT_ORDINAL

# The page-fallback reader must be at least as sensitive as the Designator's
# conservation denominator. This literal mirrors `structure.SECONDARY_MARGIN`;
# the unit test exercises the faint band between the primary and secondary
# thresholds, where the circular fixture path used to invent a blank reading.
PAGE_FALLBACK_INK_MARGIN: Final = 2

# The closed vocabulary of reading passes. Named here rather than left to the
# caller's string, because every value outside it fails *silently and in the
# worst direction*: an unrecognised `pass_kind` falls through to the
# establishing branch below, so a misspelt `"lectio_prior"` would serve Pass B's
# own text as the Pass-A draft and publish a `self_revision` of nothing at all.
# A refusal is the only reading of that a record can carry.
PASS_KINDS: Final = frozenset({"perlectio", "lectio-nuda", "lectio-prior", "primed-without-prior"})


class LectioResult(TypedDict):
    text: str
    stop_reason: str | None


class DeliveredPixels(TypedDict):
    region_images: list[bytes]
    page_render_images: list[bytes]


class Reader(Protocol):
    def read(
        self,
        dossier: dict[str, Any],
        *,
        pass_kind: str,
        delivered_pixels: DeliveredPixels | None = None,
    ) -> LectioResult:
        """Produce one Lectio over one dossier."""


class FixtureReader:
    """Reads declared fixture text for ordinary acts and inspects pixels before
    reporting that a minted page-fallback act is empty."""

    def __init__(self, fixture: dict[str, Any], scenario: str):
        self._fixture = fixture
        self._scenario = scenario

    def read(
        self,
        dossier: dict[str, Any],
        *,
        pass_kind: str,
        delivered_pixels: DeliveredPixels | None = None,
    ) -> LectioResult:
        if pass_kind not in PASS_KINDS:
            raise ContractError(
                f"unknown Perlector pass kind {pass_kind!r}; a pass this reader cannot name "
                f"would be served as the establishing read, not refused"
            )
        act_key = dossier["act_key"]
        return {
            "text": self._reading_text(
                dossier, pass_kind=pass_kind, delivered_pixels=delivered_pixels
            ),
            "stop_reason": self._declared_stop_reason(act_key),
        }

    def _reading_text(
        self,
        dossier: dict[str, Any],
        *,
        pass_kind: str,
        delivered_pixels: DeliveredPixels | None,
    ) -> str:
        act_key = dossier["act_key"]
        if self._is_page_fallback(dossier):
            # Identity, not the review-facing key, proves this dossier belongs
            # to the reserved whole-page fallback act. A key can drift or be
            # forged; the derived identity binds the page and rectangle.
            return self._observed_page_fallback_text(dossier, delivered_pixels)
        for act in self._fixture["act"]:
            if act["key"] == act_key:
                if pass_kind == "lectio-prior":
                    return self._declared_prior_reading(act_key)
                if pass_kind == "audit-reproof":
                    for reproof in self._fixture.get("audit_reproof", []):
                        if reproof["scenario"] == self._scenario and reproof["act_key"] == act_key:
                            return reproof["text"]
                    return self._confirmed_unchanged_text(dossier)
                return act["text"]
        raise KeyError(f"the fixture declares no act {act_key!r}")

    @staticmethod
    def _confirmed_unchanged_text(dossier: dict[str, Any]) -> str:
        """A re-proof with no declared change reports exactly what it was shown.

        Pass C is span-scoped: it re-examines one flagged location, never the
        whole act. A fixture with nothing declared for that location cannot
        fall back to the act's original text -- that would silently rewrite a
        no-readable-text act back to full prose, contradicting the whole-act
        gap its empty reading already carries. Absent a declared change, the
        honest report is "confirmed unchanged".
        """
        text = dossier.get("semi_final_text")
        if not isinstance(text, str):
            raise ContractError(
                "the fixture Perlector received an audit re-proof dossier without its "
                "semi-final text; a re-proof cannot confirm what it was not shown"
            )
        return text

    def _observed_page_fallback_text(
        self, dossier: dict[str, Any], delivered_pixels: DeliveredPixels | None
    ) -> str:
        """Return empty only after every delivered crop is observed ink-free.

        This fixture has no transcription engine, so ink cannot yield invented
        text. It is a named refusal instead: a real Perlector would read those
        pixels, while this one can only distinguish a proved empty crop from a
        crop that needs that real reader.
        """
        if delivered_pixels is None:
            raise ContractError(
                "the fixture Perlector received a page-fallback dossier without its pixels; "
                "blankness cannot be inferred from the fallback identity"
            )
        region_images = delivered_pixels.get("region_images")
        page_render_images = delivered_pixels.get("page_render_images")
        regions = dossier.get("regions")
        renders = dossier.get("page_renders")
        if (
            not isinstance(region_images, list)
            or not isinstance(regions, list)
            or len(region_images) != len(regions)
            or not region_images
        ):
            raise ContractError(
                "the fixture Perlector did not receive exactly every delivered page-fallback crop"
            )
        if (
            not isinstance(page_render_images, list)
            or not isinstance(renders, list)
            or len(page_render_images) != len(renders)
            or len(page_render_images) != 1
        ):
            raise ContractError(
                "the fixture Perlector did not receive the page-fallback act's one page render"
            )

        background = self._inferred_background(page_render_images[0])
        threshold = background - PAGE_FALLBACK_INK_MARGIN
        if threshold < 0:
            raise ContractError(
                f"the fixture Perlector cannot prove page-fallback blankness from background "
                f"{background} at its {PAGE_FALLBACK_INK_MARGIN}-point ink margin"
            )
        for ordinal, image in enumerate(region_images):
            _, _, rows = self._decoded_rows(image, description=f"crop {ordinal}")
            ink_pixels = sum(value <= threshold for row in rows for value in row)
            if ink_pixels:
                raise ContractError(
                    "the fixture Perlector cannot invent a reading for ink: "
                    f"page-fallback crop {ordinal} contains {ink_pixels} pixel(s) at least "
                    f"{PAGE_FALLBACK_INK_MARGIN} levels below the inferred page background "
                    f"{background}"
                )
        return ""

    @staticmethod
    def _decoded_rows(image: bytes, *, description: str) -> tuple[int, int, list[bytearray]]:
        if not isinstance(image, bytes):
            raise ContractError(f"the fixture Perlector received a non-byte {description}")
        try:
            return grayscale_rows(image)
        except ValueError as error:
            raise ContractError(
                f"the fixture Perlector could not decode delivered {description}: {error}"
            ) from error

    def _inferred_background(self, page_image: bytes) -> int:
        """Infer paper from the delivered page render, with the same checked
        majority-paper premise used by the Designator's conservation scan."""
        width, height, rows = self._decoded_rows(page_image, description="page render")
        histogram = [0] * 256
        for row in rows:
            for value in row:
                histogram[value] += 1
        background = max(range(256), key=lambda value: histogram[value])
        counted = width * height
        total = sum(value * count for value, count in enumerate(histogram))
        if background * counted < total:
            raise ContractError(
                "the fixture Perlector cannot prove page-fallback blankness because the "
                f"delivered page render's modal pixel {background} is darker than its mean"
            )
        return background

    def _is_page_fallback(self, dossier: dict[str, Any]) -> bool:
        renders = dossier.get("page_renders")
        if not isinstance(renders, list) or len(renders) != 1:
            return False
        render = renders[0]
        if not isinstance(render, dict):
            return False
        ordinal = render.get("source_page_ordinal")
        source_page_id = render.get("source_page_id")
        for page in self._fixture["page"]:
            if page["ordinal"] != ordinal:
                continue
            page_bounds = {"x": 0, "y": 0, "w": page["width"], "h": page["height"]}
            expected = derive_act_id(source_page_id, FALLBACK_PAGE_ACT_ORDINAL, page_bounds)
            return dossier.get("act_id") == expected
        return False

    def _declared_prior_reading(self, act_key: str) -> str:
        """Pass A's declared draft for *this* scenario, or a named refusal.

        The whole table validates before any row is selected, for the reason
        `_declared_stop_reason` gives below: a row naming a scenario or act
        nobody declared, or a second row for a pair already written, would
        otherwise sit unnoticed while the first match answered. Here that would
        hand one scenario's prior draft to another scenario's `self_revision`
        measurement -- the silent cross-scenario borrow this table exists to
        end. There is deliberately no fallback: a scenario whose acts reach
        Pass A and declares no prior is a fixture gap, and an instrument
        measuring a departure from a draft nobody wrote is worse than a stop.
        """
        declared_scenarios = {scenario["name"] for scenario in self._fixture["scenario"]}
        declared_acts = {act["key"] for act in self._fixture["act"]}
        rows = self._fixture.get("prior_reading", [])
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (row["scenario"], row["act_key"])
            if key in seen:
                raise KeyError(
                    f"prior_reading declares {key!r} twice; two contradictory drafts would "
                    "publish whichever is written first and discard the other silently"
                )
            seen.add(key)
            if row["scenario"] not in declared_scenarios:
                raise KeyError(f"prior_reading row names undeclared scenario {row['scenario']!r}")
            if row["act_key"] not in declared_acts:
                raise KeyError(f"prior_reading row names undeclared act {row['act_key']!r}")
        for row in rows:
            if row["scenario"] == self._scenario and row["act_key"] == act_key:
                return row["text"]
        raise KeyError(f"the fixture declares no prior reading for {self._scenario!r}/{act_key!r}")

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
