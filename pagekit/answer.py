"""The steps of page preparation, their values, and the shape of a detector's answer.

Every step value pagekit stores, whoever set it, has the same four parts as a
detector's answer: the value, a confidence from 0 to 1 (none when a person set it), a
short plain-language sentence saying what decided it, and a list of plain-language
reasons the page should be looked at. This module defines that shape and refuses an
answer or a value that breaks it, so a detector built in another slice cannot hand
the preparation core something it would misread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# The steps in the order they apply. The first two hold one value per source image;
# the rest hold one value per page cut from it.
SOURCE_STEPS = ("orientation", "split")
PAGE_STEPS = ("skew", "page_box", "content_box", "margin")
STEPS = SOURCE_STEPS + PAGE_STEPS

ORIGINS = ("detected", "manual", "locked")
ANSWER_KEYS = frozenset({"value", "confidence", "evidence", "flags"})

# A turn of 45 degrees or more is a different orientation, not a skew.
MAX_SKEW_DEGREES = 45.0


class AnswerError(ValueError):
    """A step value or a detector answer that breaks the shape and is refused."""


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AnswerError(f"{what} must be a number, not {value!r}")
    if not math.isfinite(value):
        raise AnswerError(f"{what} must be finite, not {value!r}")
    return value


def _integer(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnswerError(f"{what} must be a whole number, not {value!r}")
    return value


def _box(value: Any, what: str) -> list[int]:
    if not isinstance(value, list | tuple) or len(value) != 4:
        raise AnswerError(f"{what} must be [left, top, right, bottom], not {value!r}")
    left, top, right, bottom = (_integer(part, what) for part in value)
    if right <= left or bottom <= top:
        raise AnswerError(f"{what} {list(value)} is empty: right and bottom are not included")
    return [left, top, right, bottom]


def _point(value: Any, what: str) -> list[float]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise AnswerError(f"{what} must be a point [x, y], not {value!r}")
    return [_number(value[0], what), _number(value[1], what)]


def validate_value(step: str, value: Any) -> Any:
    """`value` for `step` in its stored form (lists, not tuples), or AnswerError.

    The checks here need nothing but the value. Whether a cut crosses the frame or a
    box lands on the page is checked where the frame is known (pagekit.geometry).
    """
    if step == "orientation":
        turns = _integer(value, "orientation")
        if turns not in (0, 1, 2, 3):
            raise AnswerError(f"orientation must be 0, 1, 2 or 3 quarter turns, not {turns}")
        return turns
    if step == "split":
        if not isinstance(value, dict):
            raise AnswerError(f"split must be an object with 'pages', not {value!r}")
        pages = value.get("pages")
        if pages == 1 and not isinstance(pages, bool):
            if set(value) != {"pages"}:
                raise AnswerError("a one-page split takes only 'pages'")
            return {"pages": 1}
        if pages == 2 and not isinstance(pages, bool):
            if set(value) != {"pages", "cut"}:
                raise AnswerError("a two-page split takes 'pages' and 'cut'")
            cut = value["cut"]
            if not isinstance(cut, list | tuple) or len(cut) != 2:
                raise AnswerError("the cut must be two points [[x0, y0], [x1, y1]]")
            first, second = _point(cut[0], "a cut point"), _point(cut[1], "a cut point")
            if first == second:
                raise AnswerError("the cut's two points are the same point")
            return {"pages": 2, "cut": [first, second]}
        raise AnswerError(f"split pages must be 1 or 2, not {pages!r}")
    if step == "skew":
        angle = _number(value, "skew")
        if abs(angle) >= MAX_SKEW_DEGREES:
            raise AnswerError(
                f"skew {angle} degrees is not a small rotation (under {MAX_SKEW_DEGREES:g}); "
                "a larger turn belongs to orientation"
            )
        return angle
    if step == "page_box":
        return _box(value, "page box")
    if step == "content_box":
        return None if value is None else _box(value, "content box")
    if step == "margin":
        margin = _number(value, "margin")
        if margin < 0:
            raise AnswerError(f"margin must be zero or more millimetres, not {margin}")
        return margin
    raise AnswerError(f"unknown step {step!r}; the steps are {', '.join(STEPS)}")


def _flags(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        raise AnswerError(f"flags must be a list of sentences, not {value!r}")
    for flag in value:
        if not isinstance(flag, str) or not flag.strip():
            raise AnswerError(f"each flag must be a non-empty sentence, not {flag!r}")
    return list(value)


@dataclass(frozen=True)
class Answer:
    """A detector's answer for one step: value, confidence, evidence and flags."""

    value: Any
    confidence: float
    evidence: str
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "flags": list(self.flags),
        }


def validate_answer(step: str, answer: Answer | dict[str, Any]) -> Answer:
    """The detector's answer for `step`, checked and normalised, or AnswerError.

    A dict must have exactly the keys value, confidence, evidence and flags.
    """
    if isinstance(answer, Answer):
        answer = answer.to_dict()
    if not isinstance(answer, dict):
        raise AnswerError(f"a {step} answer must be an Answer or an object, not {answer!r}")
    keys = set(answer)
    if keys != ANSWER_KEYS:
        missing = sorted(ANSWER_KEYS - keys)
        extra = sorted(keys - ANSWER_KEYS)
        raise AnswerError(f"a {step} answer has missing keys {missing} and unknown keys {extra}")
    value = validate_value(step, answer["value"])
    confidence = _number(answer["confidence"], f"{step} confidence")
    if not 0 <= confidence <= 1:
        raise AnswerError(f"{step} confidence must be from 0 to 1, not {confidence}")
    evidence = answer["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        raise AnswerError(f"{step} evidence must be a non-empty sentence")
    return Answer(value, float(confidence), evidence, tuple(_flags(answer["flags"])))
