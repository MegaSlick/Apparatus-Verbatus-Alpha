"""Byte-equality with the three vendor systems, provable on a laptop.

The vendor systems ruling says each witness runs as its developers intended:
each vendor's own preprocessing, prompt bytes, message shape, generation values
and output grammar are adopted verbatim and sha-pinned, and **no vendor package
is installed in alpha**.  Those two sentences together are why this file exists.
Nothing here imports a vendor library, and nothing here reaches the network in
the gate; what it does instead is pin the vendor's bytes and arithmetic as
constants measured from the pinned sources, and fail when our tree drifts from
them.

Five of the vendor systems design's six offline tests live here:

1. **Carried-bytes digests** — every vendor string this repository carries,
   digested as it is rendered, against the digest recorded beside it with its
   vendor commit or revision.
2. **Request shape** — one wire body per chair, checked against the shape the
   design fixed, including the generation bound's arithmetic and the argmax
   pin.
4. **Resize ports** — ``common/imaging_ports.py``'s two ported vendor resize
   rules over a table of dimensions.
5. **Vendor equality** — network-gated (``-m vendor_network``, deselected by
   default): fetch the pinned files, diff the carried strings against them, and
   run the vendors' own resize functions against our ports.  Fetched into a
   temporary directory and deleted; never stored.
6. **Namespace guard** — no ``chandra-ocr`` or ``churro-ocr`` distribution is
   importable, and no vendor source sits at the repository root.

Test 3, grammar conformance, is not here: it belongs beside the parsers, in
``common/test_chandra_layout.py`` and ``common/test_churro_document.py``.

**Two boundaries shape how this file is written, and both are deliberate.**

*``common/`` may never import ``pipeline/``* (``common/README.md``, enforced by
``common/chairs/test_chairs_import_boundary.py``).  DAI's carried prompt and
generation bytes live in ``pipeline/3_attestatores/feeding.py``, so test 1 reads
that file as *source text* and lifts its literals with ``ast`` rather than
importing the stage.  Reading a file is not importing it, and the digest that
comes out is over the same bytes either way.

*The wire body is built in ``pipeline/``* for the same reason, so test 2 states
the shape as a checker — :func:`refuse_unless_vendor_request_shape` — proves the
checker refuses each way of getting the shape wrong, and runs a conforming
request through the client's own real refusal path.  The checker is public on
purpose: the Wave 2 adapter tests, which may import ``common``, call it against
the body their builder actually produces, and that is the join between this
file's specification and the wire.

**U1 and U2 are parallel units.**  The Chandra and Churro carried strings are
theirs to place, in ``common/chandra_layout.py`` and ``common/churro_document.py``.
Until those modules exist the tests that digest them are ``xfail``, conditioned
on the module being *absent* rather than on the assertion failing — so the day a
module lands, the test runs for real and a wrong byte is a red gate rather than
a silent expected failure.  Those tests look the carried strings up **by their
digest, not by a constant's name**, because the name is U1's and U2's choice and
the bytes are the vendors'.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from base64 import b64encode
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from common.imaging import encode_grayscale_png
from common.imaging_ports import (
    CHANDRA_GRID_SIZE,
    CHANDRA_MAX_SIZE,
    CHANDRA_MIN_SIZE,
    CHURRO_MAX_INLINE_IMAGE_DIM,
    resize_to_fit_churro,
    scale_to_fit_chandra,
)
from operations.serving.client import (
    ChairRequest,
    ChairRequestRefusal,
    _refuse_unbuildable_request,
)
from operations.serving.http import chat_image_bytes_all

ROOT: Final = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# The vendor pins.  Every digest below was measured from the pinned source at
# the commit or revision named beside it, offline-testable afterwards.
# --------------------------------------------------------------------------

CHANDRA_CODE_REPOSITORY: Final = "github.com/datalab-to/chandra"
CHANDRA_CODE_COMMIT: Final = "d4f7467435aa4137d9539f000ddf0b7ced3eb43f"
CHURRO_CODE_REPOSITORY: Final = "github.com/stanford-oval/Churro"
CHURRO_CODE_COMMIT: Final = "4abb17386d9656199c2776195926545fc527a691"  # tag v0.3.0
CHURRO_PAPER_COMMIT: Final = "ed09bc7fd6"
DAI_WEIGHTS_REPOSITORY: Final = "Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR"
DAI_WEIGHTS_REVISION: Final = "e371095d4ffe585f31f4974462931ddbac61ff64"

# `chandra/prompts.py` at CHANDRA_CODE_COMMIT: the file's own bytes, and the
# `OCR_LAYOUT_PROMPT` f-string as it renders (the module has no imports, so
# rendering it is executing four assignments and nothing else).
CHANDRA_PROMPTS_FILE_SHA256: Final = (
    "53101d315a9923dac2fd65bf64047a73d396c69c409b3680c753315837b151eb"
)
CHANDRA_OCR_LAYOUT_PROMPT_SHA256: Final = (
    "025935f3e1de1acdfadd4c7d581ab17eb82e8caaffef7b64962621c80b7ca9a8"
)
CHANDRA_OCR_LAYOUT_PROMPT_BYTES: Final = 2_161

# `chandra/model/util.py` and Churro's `_internal/image.py`, the two files
# `common/imaging_ports.py` ports its arithmetic from.
CHANDRA_UTIL_FILE_SHA256: Final = "a13fcd1d8830406a39a7423c42afceca793bb8a3ef0181130ed4dd54263f1774"
CHURRO_IMAGE_FILE_SHA256: Final = "0708d2efe879b9b2387616e7283f3a21069c3dc79666a67bf6cef13c7d1dacd9"
CHURRO_PRESETS_FILE_SHA256: Final = (
    "2ac4f259d0591554d1c56865a759cd45bc17ac32fe2d9016131b448592abc5a1"
)
CHURRO_SPECS_FILE_SHA256: Final = "70abb3e54718fb0f37a1111363fe91afe0587d276ad26fb68de232f865716043"
# `ocr/systems/finetuned_ocr.py` at CHURRO_PAPER_COMMIT, the paper-era harness
# file whose module-level `SYSTEM_MESSAGE` is the `paper-harness-ed09bc7`
# variant.  Note the `ocr/systems/` prefix: the design cites this string as
# `finetuned_ocr.py:17`, and that bare path does not exist in the repository.
CHURRO_FINETUNED_OCR_FILE_SHA256: Final = (
    "29c414cd0cb6e4bec21dbf8f935d9768989259e267b95daad142612c1bb36ac1"
)

CHURRO_MODEL_ID: Final = "stanford-oval/churro-3B"

# Churro's system message, per prompt variant.  `registry-v0.3.0` is what
# `resolve_ocr_profile("stanford-oval/churro-3B")` answers at the pinned tag and
# is what we send; `paper-harness-ed09bc7` is the paper-era harness's own typo'd
# spelling, recorded as Stage 2 arm A4b and not sent.
CHURRO_SYSTEM_MESSAGES: Final[Mapping[str, str]] = {
    "registry-v0.3.0": "Transcribe the entirety of this historical document to XML format.",
    "paper-harness-ed09bc7": (
        "Transcribe the entiretly of this historical documents to XML format."
    ),
}
CHURRO_SYSTEM_MESSAGE_SHA256: Final[Mapping[str, str]] = {
    "registry-v0.3.0": "13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d",
    "paper-harness-ed09bc7": "dd7408ca72cf94f724b0522806427533a746f08cfa0cfd047e0272ebc4c4b489",
}

# DAI's three carried files at DAI_WEIGHTS_REVISION, by source-file digest.
# `generation_config.json`'s digest is over the file's JSON framing, so only its
# nine values can be compared offline; the network arm compares the bytes.
DAI_CARRIED_FILE_SHA256: Final[Mapping[str, str]] = {
    "system.txt": "b4e7d61d4f27f0aa46ba597ebfac3925b3ed87e72583def4bce2bd4f0393c333",
    "query.txt": "3a5cd8eb3263f2511d207f49f9933b1cf184e95fd7a9534871207d8d8b6a3489",
    "generation_config.json": ("f4cd2d54597a1a3cb38ac78d5cb275d06f6fd660fef52ee444a58d81297ff027"),
}
DAI_CARRIED_FILE_BYTES: Final[Mapping[str, int]] = {
    "system.txt": 206,
    "query.txt": 33,
    "generation_config.json": 243,
}
DAI_GENERATION_CONFIG: Final[Mapping[str, Any]] = {
    "bos_token_id": 151_643,
    "do_sample": True,
    "eos_token_id": [151_645, 151_643],
    "pad_token_id": 151_643,
    "repetition_penalty": 1.05,
    "temperature": 0.1,
    "top_k": 1,
    "top_p": 0.001,
    "transformers_version": "5.2.0",
}

FEEDING_SOURCE: Final = ROOT / "pipeline/3_attestatores/feeding.py"
ATTESTATORES_RUN_SOURCE: Final = ROOT / "pipeline/3_attestatores/run.py"

_CHANDRA_LAYOUT_PENDING: Final = importlib.util.find_spec("common.chandra_layout") is None
_CHURRO_DOCUMENT_PENDING: Final = importlib.util.find_spec("common.churro_document") is None
_PENDING_REASON: Final = "U1/U2 pending: the module carrying these vendor bytes does not exist yet"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# How deep into a module-level container the string walk below goes.  Six is
# far past any shape a carried-bytes table has taken (U2's is two: variant name,
# then field) and shallow enough that a pathological object cannot turn the walk
# into the test's runtime.
_MAX_CARRIED_DEPTH: Final = 6


def _module_string_constants(module: ModuleType) -> dict[str, str]:
    """Every ``str`` a module carries at module level, by the path that reaches it.

    Bare module-level constants *and* the strings nested inside module-level
    mappings, sequences and sets all count, because placing the bytes is the
    carrying unit's choice and both shapes are already in use: U1 carries
    Chandra's prompt as a bare ``OCR_LAYOUT_PROMPT``, while U2 carries both
    Churro system messages inside a per-variant table beside their provenance
    (``CHURRO_PROMPT_VARIANTS[variant]["system"]``).  A walk that stopped at bare
    strings would fail this file's own tests on correct vendor bytes.

    Looked up this way rather than by an agreed constant name because the name
    belongs to whichever unit places the string and the bytes belong to the
    vendor.  A rename or a reshuffle stays green; a changed byte does not.
    """
    found: dict[str, str] = {}
    walked: set[int] = set()

    def walk(label: str, value: Any, depth: int) -> None:
        if isinstance(value, str):
            found[label] = value
            return
        if depth >= _MAX_CARRIED_DEPTH or id(value) in walked:
            return
        if isinstance(value, Mapping):
            walked.add(id(value))
            for key, item in value.items():
                walk(f"{label}[{key!r}]", item, depth + 1)
        elif isinstance(value, (set, frozenset)):
            walked.add(id(value))
            for item in sorted(value, key=repr):
                walk(f"{label}{{{item!r}}}", item, depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            walked.add(id(value))
            for index, item in enumerate(value):
                walk(f"{label}[{index}]", item, depth + 1)

    for name, value in vars(module).items():
        if not name.startswith("__"):
            walk(name, value, 0)
    return found


# --------------------------------------------------------------------------
# Test 1 — carried-bytes digests
# --------------------------------------------------------------------------


@pytest.mark.xfail(condition=_CHANDRA_LAYOUT_PENDING, reason=_PENDING_REASON, strict=False)
def test_the_carried_chandra_prompt_is_the_vendors_own_rendered_bytes():
    """`OCR_LAYOUT_PROMPT` as `chandra/prompts.py` renders it at the pinned commit."""
    module = importlib.import_module("common.chandra_layout")
    carried = {
        name: value
        for name, value in _module_string_constants(module).items()
        if _digest(value) == CHANDRA_OCR_LAYOUT_PROMPT_SHA256
    }

    assert carried, (
        "no string carried by common/chandra_layout.py — at module level or "
        "inside a module-level table — digests to "
        f"{CHANDRA_OCR_LAYOUT_PROMPT_SHA256}, the rendered OCR_LAYOUT_PROMPT of "
        f"{CHANDRA_CODE_REPOSITORY}/chandra/prompts.py at {CHANDRA_CODE_COMMIT}. "
        "The prompt bytes are carried third-party content and may not be edited, "
        "reflowed, or reassembled: changing one character changes the trained "
        "request framing."
    )
    for value in carried.values():
        assert len(value.encode("utf-8")) == CHANDRA_OCR_LAYOUT_PROMPT_BYTES
        # The parse layer reads `data-bbox` as four integers on a 0-1000 scale.
        # That is only true while the prompt keeps asking for that scale.
        assert "normalized 0-1000" in value
        assert "data-bbox" in value and "data-label" in value

    source = (ROOT / "common/chandra_layout.py").read_text(encoding="utf-8")
    assert CHANDRA_OCR_LAYOUT_PROMPT_SHA256 in source, (
        "the carried Chandra prompt's digest is not recorded beside it in common/chandra_layout.py"
    )
    assert CHANDRA_CODE_COMMIT in source, (
        "the carried Chandra prompt does not name the vendor commit it was taken from"
    )


@pytest.mark.xfail(condition=_CHURRO_DOCUMENT_PENDING, reason=_PENDING_REASON, strict=False)
def test_the_carried_churro_system_messages_are_the_vendors_own_bytes_per_variant():
    """Both prompt variants, digested, and the sent one distinguishable from the arm."""
    module = importlib.import_module("common.churro_document")
    constants = _module_string_constants(module)
    digests = {name: _digest(value) for name, value in constants.items()}

    for variant, expected in CHURRO_SYSTEM_MESSAGE_SHA256.items():
        assert expected in digests.values(), (
            "no string carried by common/churro_document.py — at module level or "
            f"inside a module-level table — digests to {expected}, "
            f"the {variant!r} Churro system message from {CHURRO_CODE_REPOSITORY}. "
            "The two variants differ by a vendor typo and one of them is what the "
            "fine-tune was trained on; neither may be normalized into the other."
        )
        assert _digest(CHURRO_SYSTEM_MESSAGES[variant]) == expected

    source = (ROOT / "common/churro_document.py").read_text(encoding="utf-8")
    for variant, expected in CHURRO_SYSTEM_MESSAGE_SHA256.items():
        assert expected in source, (
            f"the {variant!r} Churro system message's digest is not recorded beside it"
        )
    assert CHURRO_CODE_COMMIT in source and CHURRO_PAPER_COMMIT in source, (
        "the two carried Churro system messages do not both name the vendor commit "
        "they were taken from"
    )


def _feeding_literal(function_name: str) -> Any:
    """The literal a `feeding.py` accessor returns, lifted without importing it.

    ``common/`` may not import ``pipeline/``.  The digest of a string literal is
    the same whether the module was executed or parsed, so the parse is used.
    """
    tree = ast.parse(FEEDING_SOURCE.read_text(encoding="utf-8"), filename=str(FEEDING_SOURCE))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            returns = [child for child in node.body if isinstance(child, ast.Return)]
            assert len(returns) == 1, (
                f"feeding.{function_name} no longer returns exactly one literal; this "
                "test lifts its carried bytes statically and cannot follow a branch"
            )
            return ast.literal_eval(returns[0].value)
    raise AssertionError(f"feeding.py no longer defines {function_name}")


def _feeding_docstring(function_name: str) -> str:
    tree = ast.parse(FEEDING_SOURCE.read_text(encoding="utf-8"), filename=str(FEEDING_SOURCE))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"feeding.py no longer defines {function_name}")


def test_the_carried_dai_prompt_bytes_digest_to_the_pinned_revisions_files():
    """`system.txt` and `query.txt` at the pinned revision, byte for byte."""
    prompt = _feeding_literal("dai_prompt")
    assert set(prompt) == {"system", "user"}

    for field, filename in (("system", "system.txt"), ("user", "query.txt")):
        raw = prompt[field].encode("utf-8")
        assert len(raw) == DAI_CARRIED_FILE_BYTES[filename], (
            f"DAI's carried {filename} is {len(raw)} bytes, not "
            f"{DAI_CARRIED_FILE_BYTES[filename]}; the trailing newline is part of "
            "the trained request framing and is carried deliberately"
        )
        assert hashlib.sha256(raw).hexdigest() == DAI_CARRIED_FILE_SHA256[filename], (
            f"DAI's carried {filename} no longer digests to the file at "
            f"{DAI_WEIGHTS_REPOSITORY}@{DAI_WEIGHTS_REVISION}"
        )

    docstring = _feeding_docstring("dai_prompt")
    assert DAI_WEIGHTS_REVISION in docstring
    for filename in ("system.txt", "query.txt"):
        assert DAI_CARRIED_FILE_SHA256[filename] in docstring, (
            f"DAI's {filename} digest is not recorded beside the bytes it describes"
        )


def test_the_carried_dai_generation_values_are_the_shipped_configuration():
    """The nine shipped values, and the source file's digest recorded beside them."""
    generation = _feeding_literal("dai_generation")

    assert generation == dict(DAI_GENERATION_CONFIG), (
        "DAI's carried generation values are no longer the shipped "
        "generation_config.json at the pinned revision. These are the vendor's "
        "decoding policy, not ours (GOVERNANCE 7); `do_sample` stays true and "
        "`temperature` stays 0.1 even though the manager forces temperature 0 on "
        "the wire, because what is carried is the record of what shipped."
    )
    # `top_k: 1` makes 0.1 argmax-equivalent; that equivalence is what lets the
    # reading of record stay at temperature 0 without departing from the vendor.
    assert generation["top_k"] == 1

    docstring = _feeding_docstring("dai_generation")
    assert DAI_CARRIED_FILE_SHA256["generation_config.json"] in docstring, (
        "the shipped generation_config.json's digest is not recorded beside the "
        "values lifted out of it"
    )
    # The revision itself is recorded once, on `dai_prompt`, and this docstring
    # cites that citation rather than repeating it. Both halves are asserted so
    # the reference cannot be left dangling by an edit to either.
    assert "dai_prompt" in docstring
    assert DAI_WEIGHTS_REVISION in _feeding_docstring("dai_prompt")


def test_the_vendor_pins_this_file_states_are_internally_consistent():
    """A pin nobody can re-derive is a number, not a measurement.

    The two Churro system messages are short enough to carry here in full, so
    their digests are re-derived rather than trusted, and the pair is asserted
    distinct — they differ only by a vendor typo, and a normalization that
    collapsed them would silently send the arm's bytes.
    """
    for variant, message in CHURRO_SYSTEM_MESSAGES.items():
        assert _digest(message) == CHURRO_SYSTEM_MESSAGE_SHA256[variant]
    assert len(set(CHURRO_SYSTEM_MESSAGES.values())) == 2
    assert len(set(CHURRO_SYSTEM_MESSAGE_SHA256.values())) == 2


def test_the_carried_dai_generation_values_rebuild_the_shipped_file_byte_for_byte():
    """The one file digest that looked unprovable offline, and is not.

    ``feeding.dai_generation`` returns the nine values re-typed as a mapping,
    and its docstring says so: the digest recorded beside them is over the
    vendor's *file*, whose remaining bytes are its JSON framing, so the
    docstring calls it a source-file digest rather than a claim about the
    mapping. That framing turns out to be exactly ``json.dumps(indent=2)`` with
    a trailing newline, which makes the file reconstructable from the carried
    values with no network and no stored vendor file — so the third DAI digest
    the design's Offline test 1 names is checked here for real rather than left
    to the network arm.

    Key order is part of it. ``json.dumps`` writes insertion order, so this
    passes only while the carried mapping is in the vendor's own order, and a
    reordering — invisible to a value comparison — is caught here.
    """
    rebuilt = (json.dumps(_feeding_literal("dai_generation"), indent=2) + "\n").encode("utf-8")

    assert len(rebuilt) == DAI_CARRIED_FILE_BYTES["generation_config.json"]
    assert hashlib.sha256(rebuilt).hexdigest() == DAI_CARRIED_FILE_SHA256["generation_config.json"]


# --------------------------------------------------------------------------
# Test 2 — request shape
# --------------------------------------------------------------------------


class VendorRequestShapeRefusal(AssertionError):
    """The wire body departs from the vendor system the chair is meant to run."""


# The prompt shapes and message framing the vendor systems design fixed, so that
# parallel units agree without talking.  `system_content_parts` records the
# vendors' own shape for the system turn: a one-element list of `{type: text}`
# parts for DAI and Churro (it renders identically to a bare string, and the
# vendors send the list).
CHAIR_VENDOR_SYSTEMS: Final[Mapping[str, Mapping[str, Any]]] = {
    "designator_structure": {
        "adapter": "chandra.v1",
        "prompt_fields": ("user",),
        "user_parts": ("image_url", "text"),
        "generation_ceiling": 12_384,
        "ceiling_source": "chandra/settings.py:14 Settings.MAX_OUTPUT_TOKENS",
        "required_generation_sent": ("max_tokens",),
        "allowed_generation_sent": ("max_tokens",),
    },
    "attestator_1": {
        "adapter": "chandra.v1",
        "prompt_fields": ("user",),
        "user_parts": ("image_url", "text"),
        "generation_ceiling": 12_384,
        "ceiling_source": "chandra/settings.py:14 Settings.MAX_OUTPUT_TOKENS",
        "required_generation_sent": ("max_tokens",),
        "allowed_generation_sent": ("max_tokens",),
    },
    "attestator_2": {
        "adapter": "dai.v1",
        "prompt_fields": ("system", "user"),
        "user_parts": ("image_url", "text"),
        "generation_ceiling": 1_024,
        "ceiling_source": "model card README, `max_new_tokens=1024`",
        "required_generation_sent": ("max_tokens", "repetition_penalty", "top_k", "top_p"),
        "allowed_generation_sent": ("max_tokens", "repetition_penalty", "top_k", "top_p"),
    },
    "attestator_3": {
        "adapter": "churro.v1",
        "prompt_fields": ("system",),
        "user_parts": ("image_url",),
        "generation_ceiling": 20_000,
        "ceiling_source": "utils/llm/models.py COMPLETION_TOKENS_FOR_STANDARD_MODELS",
        "required_generation_sent": ("max_tokens", "repetition_penalty"),
        "allowed_generation_sent": ("max_tokens", "repetition_penalty"),
    },
}

# The manager owns these and the client refuses a caller that names them, so the
# argmax pin is checked against the *composed* body: what generation_sent asks
# for, plus what the manager forces.
MANAGER_FORCED: Final[Mapping[str, Any]] = {"temperature": 0, "seed": 0}


def vendor_generation_bound(
    chair: str, *, max_model_len: int, image_tokens: int, prompt_tokens: int
) -> int:
    """The `max_tokens` this chair may ask for on this row, or refuse.

    ``min(vendor ceiling, row context - image - prompt)``.  The vendor's own
    ceiling is the first term because that is what its harness sends; the second
    is ours, because two of the three vendors overrun their own container and
    say nothing (Chandra's 12,384 plus a 6,045-token A4 image exceeds its own
    launcher's 18,000-token context).
    """
    if chair not in CHAIR_VENDOR_SYSTEMS:
        raise VendorRequestShapeRefusal(f"{chair!r} is not a vendor-system chair")
    headroom = max_model_len - image_tokens - prompt_tokens
    if headroom <= 0:
        raise VendorRequestShapeRefusal(
            f"{chair} has no generation headroom on a {max_model_len}-token row: "
            f"{image_tokens} image + {prompt_tokens} prompt tokens leave {headroom}"
        )
    return min(CHAIR_VENDOR_SYSTEMS[chair]["generation_ceiling"], headroom)


def _part_types(content: object, *, turn: str) -> tuple[str, ...]:
    """The `type` of each content part, or a refusal naming which turn is wrong.

    ``turn`` is not decoration: both turns go through here, and a message that
    always said "user turn" would report the wrong half of the body — which is
    how a shape defect gets chased in the wrong file.
    """
    if not isinstance(content, list):
        raise VendorRequestShapeRefusal(
            f"the {turn} turn's content is {type(content).__name__}, not a list of parts"
        )
    types: list[str] = []
    for part in content:
        if not isinstance(part, Mapping) or "type" not in part:
            raise VendorRequestShapeRefusal(f"a {turn} turn content part is not a typed object")
        types.append(str(part["type"]))
    return tuple(types)


def refuse_unless_vendor_request_shape(
    chair: str,
    request: ChairRequest,
    *,
    max_model_len: int,
    image_tokens: int,
    prompt_tokens: int,
    forced_generation: Mapping[str, Any] = MANAGER_FORCED,
) -> None:
    """Refuse a wire body that is not the vendor system this chair runs.

    Public so the Wave 2 adapter tests, which live under ``pipeline/`` and may
    import ``common``, can hold their real builder to the same statement this
    file proves offline.  Raises :class:`VendorRequestShapeRefusal`; returns
    ``None`` when the body conforms.
    """
    spec = CHAIR_VENDOR_SYSTEMS.get(chair)
    if spec is None:
        raise VendorRequestShapeRefusal(f"{chair!r} is not a vendor-system chair")

    messages: Sequence[Mapping[str, object]] = request.messages
    roles = tuple(str(message.get("role")) for message in messages)
    wants_system = "system" in spec["prompt_fields"]
    expected_roles = ("system", "user") if wants_system else ("user",)
    if roles != expected_roles:
        raise VendorRequestShapeRefusal(
            f"{chair} ({spec['adapter']}) sends turns {roles}, not {expected_roles}"
        )

    if wants_system:
        system_content = messages[0].get("content")
        if _part_types(system_content, turn="system") != ("text",):
            raise VendorRequestShapeRefusal(
                f"{chair} does not send its system turn as one text part, the vendors' own shape"
            )
        if not str(system_content[0].get("text", "")).strip():  # type: ignore[index]
            raise VendorRequestShapeRefusal(f"{chair} sends a blank system turn")

    user_types = _part_types(messages[-1].get("content"), turn="user")
    if user_types != tuple(spec["user_parts"]):
        raise VendorRequestShapeRefusal(
            f"{chair} sends user parts {user_types}, not {tuple(spec['user_parts'])}. "
            "The image part's index is load-bearing: text before image amplifies the "
            "text-over-image bias every vendor's own caller avoids."
        )

    sent = dict(request.generation_sent)
    missing = [key for key in spec["required_generation_sent"] if key not in sent]
    if missing:
        raise VendorRequestShapeRefusal(f"{chair} sends no {missing}; the wire would be unbounded")
    extra = sorted(set(sent) - set(spec["allowed_generation_sent"]))
    if extra:
        raise VendorRequestShapeRefusal(f"{chair} sends {extra}, which its vendor system does not")

    bound = vendor_generation_bound(
        chair,
        max_model_len=max_model_len,
        image_tokens=image_tokens,
        prompt_tokens=prompt_tokens,
    )
    if sent["max_tokens"] != bound:
        raise VendorRequestShapeRefusal(
            f"{chair} asks for max_tokens={sent['max_tokens']}, not the "
            f"{bound} its ceiling ({spec['ceiling_source']}) and this row allow"
        )

    composed = {**sent, **dict(forced_generation)}
    if composed.get("top_k") == 1 and composed.get("temperature") != 0:
        raise VendorRequestShapeRefusal(
            f"{chair} sends top_k=1 without temperature 0; a top-1 sample at a "
            "nonzero temperature is not the argmax reading of record"
        )


def _png_and_uri(width: int = 4, height: int = 3) -> tuple[str, str]:
    """One tiny valid PNG as a data URI, with its digest."""
    payload = encode_grayscale_png(width, height, [bytearray([160] * width) for _ in range(height)])
    uri = "data:image/png;base64," + b64encode(payload).decode("ascii")
    return uri, hashlib.sha256(payload).hexdigest()


def _conforming_request(chair: str, *, max_tokens: int) -> ChairRequest:
    spec = CHAIR_VENDOR_SYSTEMS[chair]
    uri, digest = _png_and_uri()
    image_part = {"type": "image_url", "image_url": {"url": uri}}
    text_part = {"type": "text", "text": "the carried vendor prompt bytes"}
    user_content = [image_part if kind == "image_url" else text_part for kind in spec["user_parts"]]
    messages: list[Mapping[str, object]] = []
    if "system" in spec["prompt_fields"]:
        messages.append(
            {"role": "system", "content": [{"type": "text", "text": "carried system bytes"}]}
        )
    messages.append({"role": "user", "content": user_content})
    generation_sent: dict[str, Any] = {"max_tokens": max_tokens}
    if chair == "attestator_2":
        generation_sent.update({"repetition_penalty": 1.05, "top_k": 1, "top_p": 0.001})
    if chair == "attestator_3":
        generation_sent["repetition_penalty"] = 1.05
    return ChairRequest(
        kind="chat-completions",
        messages=tuple(messages),
        image_sha256s=(digest,),
        generation_declared={},
        generation_sent=generation_sent,
    )


def _serving_rows() -> dict[tuple[str, str], Mapping[str, Any]]:
    catalogue = tomllib.loads((ROOT / "config/serving_recipes_real.toml").read_text("utf-8"))
    return {(row["chair"], row["tier"]): row for row in catalogue["profiles"]}


ROW_TIER: Final = "generic-24gb"
# One representative cost per chair, small enough to leave headroom on any row
# this catalogue has held.  The arithmetic under test is the `min`, not these
# two numbers: U14 re-measures the real prompt costs and U15 moves the rows, and
# neither may change what `min(ceiling, context - image - prompt)` means.
SAMPLE_IMAGE_TOKENS: Final = 1_200
SAMPLE_PROMPT_TOKENS: Final = 300


@pytest.mark.parametrize("chair", sorted(CHAIR_VENDOR_SYSTEMS))
def test_each_chairs_conforming_body_satisfies_both_this_shape_and_the_clients_own(chair: str):
    """The shape the design fixed, and the client's real refusal path, agree."""
    row = _serving_rows()[(chair, ROW_TIER)]
    bound = vendor_generation_bound(
        chair,
        max_model_len=row["max_model_len"],
        image_tokens=SAMPLE_IMAGE_TOKENS,
        prompt_tokens=SAMPLE_PROMPT_TOKENS,
    )
    request = _conforming_request(chair, max_tokens=bound)

    refuse_unless_vendor_request_shape(
        chair,
        request,
        max_model_len=row["max_model_len"],
        image_tokens=SAMPLE_IMAGE_TOKENS,
        prompt_tokens=SAMPLE_PROMPT_TOKENS,
    )
    # The client refuses an unbuildable request before a byte is built or sent.
    # A body this file calls conforming must survive that too, or the shape is a
    # specification of something the wire could never carry.
    _refuse_unbuildable_request(request)
    assert len(chat_image_bytes_all({"messages": list(request.messages)})) == 1
    assert "temperature" not in request.generation_sent
    assert "seed" not in request.generation_sent


@pytest.mark.parametrize("chair", sorted(CHAIR_VENDOR_SYSTEMS))
def test_the_generation_bound_is_the_min_of_the_vendor_ceiling_and_the_rows_headroom(chair: str):
    ceiling = CHAIR_VENDOR_SYSTEMS[chair]["generation_ceiling"]

    # Roomy row: the vendor's own ceiling is what is sent.
    assert (
        vendor_generation_bound(
            chair, max_model_len=ceiling + 5_000, image_tokens=1_000, prompt_tokens=100
        )
        == ceiling
    )
    # Tight row: the row's headroom is what is sent, and it is exact.
    assert vendor_generation_bound(
        chair, max_model_len=4_096, image_tokens=3_000, prompt_tokens=96
    ) == min(ceiling, 1_000)
    # No headroom at all is a refusal, never a zero or negative bound.
    with pytest.raises(VendorRequestShapeRefusal, match="no generation headroom"):
        vendor_generation_bound(chair, max_model_len=4_096, image_tokens=4_000, prompt_tokens=96)


def _mutations(chair: str, bound: int) -> list[tuple[str, str, ChairRequest]]:
    """One way of getting each rule wrong, per chair, with the reason expected.

    The expected reason is carried beside each case on purpose: a mutation that
    happens to be refused by some *other* rule would otherwise read as coverage
    of the rule it was written for, and the checker could lose a clause without
    a single test going red.
    """
    spec = CHAIR_VENDOR_SYSTEMS[chair]
    uri, _digest_unused = _png_and_uri()
    image_part = {"type": "image_url", "image_url": {"url": uri}}
    text_part = {"type": "text", "text": "the carried vendor prompt bytes"}
    base = _conforming_request(chair, max_tokens=bound)

    def rebuilt(**changes: Any) -> ChairRequest:
        fields: dict[str, Any] = {
            "kind": base.kind,
            "messages": base.messages,
            "image_sha256s": base.image_sha256s,
            "generation_declared": base.generation_declared,
            "generation_sent": dict(base.generation_sent),
        }
        fields.update(changes)
        return ChairRequest(**fields)

    cases: list[tuple[str, str, ChairRequest]] = []

    reversed_user = list(reversed(list(base.messages[-1]["content"])))  # type: ignore[arg-type]
    if len(reversed_user) > 1:
        cases.append(
            (
                "text before image",
                "sends user parts",
                rebuilt(
                    messages=(*base.messages[:-1], {"role": "user", "content": reversed_user}),
                ),
            )
        )

    if "system" in spec["prompt_fields"]:
        cases.append(
            (
                "system turn as a bare string",
                "the system turn's content is str, not a list of parts",
                rebuilt(
                    messages=(
                        {"role": "system", "content": "carried system bytes"},
                        *base.messages[1:],
                    )
                ),
            )
        )
        cases.append(
            (
                "the system prompt split across two text parts",
                "does not send its system turn as one text part",
                rebuilt(
                    messages=(
                        {
                            "role": "system",
                            "content": [
                                {"type": "text", "text": "carried "},
                                {"type": "text", "text": "system bytes"},
                            ],
                        },
                        *base.messages[1:],
                    )
                ),
            )
        )
        cases.append(
            (
                "a blank system turn",
                "sends a blank system turn",
                rebuilt(
                    messages=(
                        {"role": "system", "content": [{"type": "text", "text": "  "}]},
                        *base.messages[1:],
                    )
                ),
            )
        )
    else:
        cases.append(
            (
                "a system turn the vendor never sends",
                "sends turns",
                rebuilt(
                    messages=(
                        {"role": "system", "content": [{"type": "text", "text": "invented"}]},
                        *base.messages,
                    )
                ),
            )
        )

    if spec["user_parts"] == ("image_url",):
        cases.append(
            (
                "a user text part the vendor never sends",
                "sends user parts",
                rebuilt(
                    messages=(
                        *base.messages[:-1],
                        {"role": "user", "content": [image_part, text_part]},
                    )
                ),
            )
        )

    sent = dict(base.generation_sent)
    cases.append(
        (
            "no max_tokens at all",
            "the wire would be unbounded",
            rebuilt(
                generation_sent={key: value for key, value in sent.items() if key != "max_tokens"}
            ),
        )
    )
    cases.append(
        (
            "max_tokens past the row",
            "asks for max_tokens",
            rebuilt(generation_sent={**sent, "max_tokens": bound + 1}),
        )
    )
    cases.append(
        (
            "an unvendored generation key",
            "which its vendor system does not",
            rebuilt(generation_sent={**sent, "min_p": 0.1}),
        )
    )
    return cases


@pytest.mark.parametrize("chair", sorted(CHAIR_VENDOR_SYSTEMS))
def test_every_way_of_departing_from_the_vendor_system_is_refused(chair: str):
    """A checker that cannot refuse is not a check."""
    row = _serving_rows()[(chair, ROW_TIER)]
    bound = vendor_generation_bound(
        chair,
        max_model_len=row["max_model_len"],
        image_tokens=SAMPLE_IMAGE_TOKENS,
        prompt_tokens=SAMPLE_PROMPT_TOKENS,
    )
    cases = _mutations(chair, bound)
    assert len(cases) >= 5, f"{chair} is exercised by only {len(cases)} departures"

    for label, expected_reason, request in cases:
        with pytest.raises(VendorRequestShapeRefusal) as refusal:
            refuse_unless_vendor_request_shape(
                chair,
                request,
                max_model_len=row["max_model_len"],
                image_tokens=SAMPLE_IMAGE_TOKENS,
                prompt_tokens=SAMPLE_PROMPT_TOKENS,
            )
        assert expected_reason in str(refusal.value), (
            f"{chair}: {label!r} was refused, but for {str(refusal.value)!r} rather than "
            f"the {expected_reason!r} rule this case exists to exercise"
        )


def test_a_top_one_sample_at_a_nonzero_temperature_is_refused():
    """DAI ships `top_k 1`; that is argmax only while temperature is 0."""
    chair = "attestator_2"
    row = _serving_rows()[(chair, ROW_TIER)]
    bound = vendor_generation_bound(
        chair,
        max_model_len=row["max_model_len"],
        image_tokens=SAMPLE_IMAGE_TOKENS,
        prompt_tokens=SAMPLE_PROMPT_TOKENS,
    )
    request = _conforming_request(chair, max_tokens=bound)

    with pytest.raises(VendorRequestShapeRefusal, match="argmax reading of record"):
        refuse_unless_vendor_request_shape(
            chair,
            request,
            max_model_len=row["max_model_len"],
            image_tokens=SAMPLE_IMAGE_TOKENS,
            prompt_tokens=SAMPLE_PROMPT_TOKENS,
            forced_generation={"temperature": 0.1, "seed": 0},
        )


def test_a_caller_that_names_a_manager_owned_field_is_refused_by_the_client():
    """The argmax pin is checked on the composed body because of exactly this."""
    chair = "attestator_3"
    row = _serving_rows()[(chair, ROW_TIER)]
    bound = vendor_generation_bound(
        chair,
        max_model_len=row["max_model_len"],
        image_tokens=SAMPLE_IMAGE_TOKENS,
        prompt_tokens=SAMPLE_PROMPT_TOKENS,
    )
    base = _conforming_request(chair, max_tokens=bound)
    request = ChairRequest(
        kind=base.kind,
        messages=base.messages,
        image_sha256s=base.image_sha256s,
        generation_declared={},
        generation_sent={**dict(base.generation_sent), "temperature": 0},
    )

    with pytest.raises(ChairRequestRefusal, match="manager-owned"):
        _refuse_unbuildable_request(request)


# --------------------------------------------------------------------------
# Test 4 — resize ports
# --------------------------------------------------------------------------

# Every entry measured against the vendors' own functions at the pinned commits
# while this file was written, and re-measured on demand by the network arm.
# Column 2 is `scale_to_fit` (Chandra), column 3 is `resize_image_to_fit`
# bounded at 2,500 (Churro).
RESIZE_TABLE: Final[tuple[tuple[tuple[int, int], tuple[int, int], tuple[int, int]], ...]] = (
    ((2480, 3508), (2100, 2968), (1767, 2500)),  # A4 at 300 dpi, the Door's render
    ((2550, 3300), (2184, 2856), (1931, 2500)),  # US Letter at 300 dpi
    ((1700, 2200), (1708, 2212), (1700, 2200)),  # Letter at 200 dpi: Chandra upscales
    ((3400, 4400), (2184, 2856), (1931, 2500)),
    ((4960, 7016), (2100, 2968), (1767, 2500)),  # A4 at 600 dpi
    ((224, 224), (224, 224), (224, 224)),  # exactly Chandra's min_pixels
    ((1, 1), (224, 224), (1, 1)),
    ((10000, 1), (22400, 28), (2500, 1)),
    ((1, 10000), (28, 22400), (1, 2500)),
    ((1792, 27), (1820, 28), (1792, 27)),
    ((1792, 28), (1792, 28), (1792, 28)),  # exactly Chandra's min_size
    ((3072, 2048), (3052, 2044), (2500, 1666)),  # exactly Chandra's max_size
    ((3073, 2048), (3052, 2044), (2500, 1666)),
    ((28, 28), (224, 224), (28, 28)),
    ((5000, 5000), (2492, 2520), (2500, 2500)),
    ((2500, 2500), (2492, 2492), (2500, 2500)),  # Churro's own ceiling, untouched
    ((2501, 2500), (2492, 2492), (2500, 2499)),  # one pixel over Churro's box
    ((2500, 2501), (2492, 2492), (2499, 2500)),
    ((2501, 1000), (2492, 1008), (2500, 999)),  # int() truncation, where round() differs
    ((100, 4000), (112, 4004), (62, 2500)),
    ((4000, 100), (4004, 112), (2500, 62)),
    ((6000, 1000), (5992, 1008), (2500, 416)),
    ((1000, 6000), (1008, 5992), (416, 2500)),
    ((3000, 2000), (2996, 1988), (2500, 1666)),
    ((2000, 3000), (1988, 2996), (1666, 2500)),
    ((300, 167), (308, 168), (300, 167)),
    ((225, 223), (224, 224), (225, 223)),
    ((180, 293), (168, 280), (180, 293)),  # below min_pixels; see the test below
)

CHANDRA_MAX_PIXELS: Final = CHANDRA_MAX_SIZE[0] * CHANDRA_MAX_SIZE[1]
CHANDRA_MIN_PIXELS: Final = CHANDRA_MIN_SIZE[0] * CHANDRA_MIN_SIZE[1]


@pytest.mark.parametrize(("source", "chandra", "churro"), RESIZE_TABLE)
def test_the_resize_ports_reproduce_the_vendor_dimensions(
    source: tuple[int, int], chandra: tuple[int, int], churro: tuple[int, int]
):
    assert scale_to_fit_chandra(*source) == chandra
    assert resize_to_fit_churro(*source) == churro


@pytest.mark.parametrize("source", [entry[0] for entry in RESIZE_TABLE])
def test_chandras_port_lands_on_the_vendor_grid_and_under_its_pixel_ceiling(
    source: tuple[int, int],
):
    """The two properties the vendor's own loop actually guarantees."""
    width, height = scale_to_fit_chandra(*source)

    assert width % CHANDRA_GRID_SIZE == 0 and height % CHANDRA_GRID_SIZE == 0, (
        "the vendor's block arithmetic always multiplies whole 28-pixel blocks back "
        "out; a result off the grid means the trim loop was not reproduced"
    )
    assert width >= CHANDRA_GRID_SIZE and height >= CHANDRA_GRID_SIZE
    assert width * height <= CHANDRA_MAX_PIXELS, (
        "the refinement loop exists to bring the block area under max_pixels and it "
        "is the only bound the engine's own max_pixels is set against"
    )


def test_the_vendors_pixel_floor_is_applied_before_rounding_and_can_be_missed():
    """A property the design overstated, pinned as the vendor actually behaves.

    The design's Offline test 4 says Chandra's output lands within
    ``[50,176, 6,291,456]``.  The upper half holds everywhere.  The lower half
    does not: ``scale_to_fit`` applies ``min_size`` to the *float* scale and
    never re-checks after rounding the sides down to whole grid blocks, so an
    ordinary small portrait page comes out under the floor.  That is the
    vendor's behaviour and it is reproduced rather than corrected — the Door
    renders at 300 dpi, so no page the pipeline presents is anywhere near this
    — but a test asserting the floor unconditionally would be asserting
    something false.
    """
    assert scale_to_fit_chandra(180, 293) == (168, 280)
    assert 168 * 280 < CHANDRA_MIN_PIXELS

    # Where the floor does bind — a page whose pixels are genuinely below it —
    # the scale-up happens and the result clears the floor.
    for source in ((1, 1), (28, 28), (224, 224), (1792, 27)):
        width, height = scale_to_fit_chandra(*source)
        assert width * height >= CHANDRA_MIN_PIXELS

    # Every page the Door actually renders is far above the floor.
    for source in ((2480, 3508), (2550, 3300), (1700, 2200), (4960, 7016)):
        width, height = scale_to_fit_chandra(*source)
        assert CHANDRA_MIN_PIXELS <= width * height <= CHANDRA_MAX_PIXELS


@pytest.mark.parametrize("source", [entry[0] for entry in RESIZE_TABLE])
def test_churros_port_only_ever_shrinks_and_fits_inside_the_vendors_box(
    source: tuple[int, int],
):
    width, height = resize_to_fit_churro(*source)

    assert width <= CHURRO_MAX_INLINE_IMAGE_DIM and height <= CHURRO_MAX_INLINE_IMAGE_DIM
    assert width <= source[0] and height <= source[1], (
        "`resize_image_to_fit` is downscale-only; a page inside the box is returned "
        "untouched and one outside it is never enlarged on the other axis"
    )
    assert width >= 1 and height >= 1
    if source[0] <= CHURRO_MAX_INLINE_IMAGE_DIM and source[1] <= CHURRO_MAX_INLINE_IMAGE_DIM:
        assert (width, height) == source


def test_the_resize_ports_refuse_a_page_with_a_side_that_is_not_a_positive_integer():
    """The one departure from the vendors, and it is a refusal, not a substitution."""
    for port in (scale_to_fit_chandra, resize_to_fit_churro):
        for bad in ((0, 10), (10, 0), (-1, 10), (10, -1)):
            with pytest.raises(ValueError, match="positive integer"):
                port(*bad)
        with pytest.raises(ValueError, match="positive integer"):
            port(True, 10)


def test_churros_truncation_is_int_and_not_round():
    """`int()`, not `round()`: a fractional pixel is dropped, never gained.

    2501 x 1000 scales by 2500/2501, which puts the height at 999.6002 — the
    vendor truncates that to 999 and rounding would raise it to 1,000. Chandra's
    port rounds in the same place and Churro's does not, so the difference is
    worth one case of its own rather than being left to the table to imply.
    """
    scale = 2500 / 2501
    assert resize_to_fit_churro(2501, 1000) == (2500, int(1000 * scale)) == (2500, 999)
    assert round(1000 * scale) == 1_000


# --------------------------------------------------------------------------
# Test 6 — namespace guard
# --------------------------------------------------------------------------


def _shadowing_import_line() -> str:
    """Where `pipeline/3_attestatores/run.py` imports the bare name `chandra`."""
    text = ATTESTATORES_RUN_SOURCE.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if re.fullmatch(r"import chandra(\s+#.*)?", line.strip()):
            return f"pipeline/3_attestatores/run.py:{number}"
    return "pipeline/3_attestatores/run.py (the bare `import chandra` is gone)"


def test_no_vendor_ocr_distribution_is_installed():
    """No vendor package in alpha — and the bare-name import makes it matter."""
    installed = {
        (distribution.metadata["Name"] or "").lower().replace("_", "-")
        for distribution in importlib.metadata.distributions()
    }
    found = sorted({"chandra-ocr", "churro-ocr"} & installed)

    assert not found, (
        f"{found} is installed. The vendor systems ruling installs no vendor package "
        "in alpha, and this one is worse than dead weight here: "
        f"{_shadowing_import_line()} imports the bare name `chandra` after putting "
        "its own directory on sys.path[0], so `pipeline/3_attestatores/chandra.py` "
        "shadows the distribution's own top-level `chandra` package. Whichever one "
        "wins, a reader cannot tell from the import which module ran."
    )


def test_no_vendor_source_sits_at_the_repository_root():
    """The five untracked vendor files U0 deleted, guarded by name and by shape."""
    modules = sorted(path.name for path in ROOT.glob("*.py"))

    assert modules == ["conftest.py"], (
        f"the repository root holds {modules}. Only conftest.py belongs there: the "
        "vendor's own __init__.py, hf.py, schema.py, util.py and vllm.py were sitting "
        "here untracked, one `git add -A` from history, and they are what made "
        f"{_shadowing_import_line()} resolvable to vendor code. Vendor bytes are "
        "fetched at boot and never stored (cleanroom/README.md)."
    )
    for name in ("chandra", "churro_ocr", "churro"):
        assert not (ROOT / name).exists(), (
            f"a top-level {name}/ directory at the repository root would be a stored "
            "vendor package under another shape"
        )


# --------------------------------------------------------------------------
# Test 5 — vendor equality, network-gated
# --------------------------------------------------------------------------

CHANDRA_RAW: Final = f"https://raw.githubusercontent.com/datalab-to/chandra/{CHANDRA_CODE_COMMIT}"
CHURRO_RAW: Final = f"https://raw.githubusercontent.com/stanford-oval/Churro/{CHURRO_CODE_COMMIT}"
CHURRO_PAPER_RAW: Final = (
    f"https://raw.githubusercontent.com/stanford-oval/Churro/{CHURRO_PAPER_COMMIT}"
)
DAI_RAW: Final = f"https://huggingface.co/{DAI_WEIGHTS_REPOSITORY}/resolve/{DAI_WEIGHTS_REVISION}"

VENDOR_FILES: Final[Mapping[str, tuple[str, str]]] = {
    "chandra_prompts.py": (f"{CHANDRA_RAW}/chandra/prompts.py", CHANDRA_PROMPTS_FILE_SHA256),
    "chandra_util.py": (f"{CHANDRA_RAW}/chandra/model/util.py", CHANDRA_UTIL_FILE_SHA256),
    "churro_presets.py": (
        f"{CHURRO_RAW}/src/churro_ocr/templates/presets.py",
        CHURRO_PRESETS_FILE_SHA256,
    ),
    "churro_specs.py": (
        f"{CHURRO_RAW}/src/churro_ocr/providers/specs.py",
        CHURRO_SPECS_FILE_SHA256,
    ),
    "churro_image.py": (
        f"{CHURRO_RAW}/src/churro_ocr/_internal/image.py",
        CHURRO_IMAGE_FILE_SHA256,
    ),
    # The paper-era arm, at its own commit.  Without this fetch the
    # `paper-harness-ed09bc7` digest is only ever checked against a literal in
    # this same file, which proves the literal self-consistent and nothing at
    # all about the vendor — so the arm this repository records as *not sent*
    # would be the one pin no measurement stands behind.
    #
    # The design names `evaluation/xml_utils.py` at this commit too, and it is
    # deliberately not fetched: this file pins no bytes from it.  Churro's
    # output grammar is U2's port, and grammar conformance is the design's
    # Offline test 3, which lives beside the parser in
    # `common/test_churro_document.py` rather than here (see this module's
    # docstring).  A fetch that asserted nothing about our tree would be
    # ceremony, not measurement.
    "churro_finetuned_ocr.py": (
        f"{CHURRO_PAPER_RAW}/ocr/systems/finetuned_ocr.py",
        CHURRO_FINETUNED_OCR_FILE_SHA256,
    ),
    "dai_system.txt": (f"{DAI_RAW}/system.txt", DAI_CARRIED_FILE_SHA256["system.txt"]),
    "dai_query.txt": (f"{DAI_RAW}/query.txt", DAI_CARRIED_FILE_SHA256["query.txt"]),
    "dai_generation_config.json": (
        f"{DAI_RAW}/generation_config.json",
        DAI_CARRIED_FILE_SHA256["generation_config.json"],
    ),
}

# Runs the vendors' own two resize functions against the table.  A subprocess,
# because it executes fetched third-party source: nothing it imports or defines
# can reach the test process, and the interpreter exits before the temporary
# directory is removed.  `churro_ocr.errors` is stubbed because the fetched
# module imports it for an exception class that this arithmetic never raises.
_VENDOR_RESIZE_SUBPROCESS: Final = '''
import importlib.util, json, sys, types

directory, table = sys.argv[1], json.loads(sys.argv[2])

package = types.ModuleType("churro_ocr")
package.__path__ = []
errors = types.ModuleType("churro_ocr.errors")
class ConfigurationError(Exception):
    pass
errors.ConfigurationError = ConfigurationError
sys.modules["churro_ocr"] = package
sys.modules["churro_ocr.errors"] = errors


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, directory + "/" + filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Page:
    """Only what the two vendor functions touch, so no pixels are allocated."""

    def __init__(self, width, height):
        self.size = (width, height)
        self.mode = "RGB"

    def resize(self, size, resample=None):
        return Page(*size)


chandra = load("vendor_chandra_util", "chandra_util.py")
churro = load("vendor_churro_image", "churro_image.py")
print(json.dumps([
    [chandra.scale_to_fit(Page(w, h)).size,
     churro.resize_image_to_fit(Page(w, h), 2500, 2500).size]
    for w, h in table
]))
'''


def _fetch(url: str, destination: Path) -> bytes:
    # Every URL here is a module constant built from a pinned https host and a
    # pinned commit or revision; none is composed from anything a test reads.
    request = urllib.request.Request(url, headers={"User-Agent": "verbatus-vendor-parity"})
    assert url.startswith("https://"), f"{url} is not https"
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    destination.write_bytes(payload)
    return payload


@pytest.mark.vendor_network
def test_the_carried_bytes_and_ports_equal_the_pinned_vendor_sources(request):
    """Fetch the pinned vendor files, diff everything against them, delete them.

    Nothing fetched here is written into the tree: the standing ruling is that
    vendor repositories are fetched at boot and never stored, and only the
    strings the design names as carried cross, with their digests recorded
    beside them.

    **The marker alone does not gate it.**  Neither gate deselects by marker in
    any way that would leave this test out: ``.githooks/check-all.sh`` runs
    pytest with no ``-m`` expression at all, and ``.githooks/check-fast.sh``
    runs ``-m "not full or scanner"``, which a test carrying only
    ``vendor_network`` satisfies through its ``not full`` half.  Both therefore
    collect it, and registering the marker would have put GitHub and Hugging
    Face on the critical path of a green laptop gate.  The run is asked what it
    selected instead, and this test runs only when that expression names this
    marker deliberately.
    """
    selection = str(request.config.getoption("-m", default="") or "")
    if "vendor_network" not in selection:
        pytest.skip(
            "network-gated vendor parity: run `pytest common/test_vendor_parity.py "
            "-m vendor_network` to re-measure the carried-bytes pins"
        )

    with tempfile.TemporaryDirectory(prefix="vendor-parity-") as directory:
        root = Path(directory)
        payloads = {}
        for name, (url, expected) in VENDOR_FILES.items():
            payload = _fetch(url, root / name)
            actual = hashlib.sha256(payload).hexdigest()
            assert actual == expected, (
                f"{url} is now {actual}, not the {expected} this repository pinned. "
                "A pinned commit whose bytes changed is a stop, not a re-pin."
            )
            payloads[name] = payload

        # --- Chandra's prompt, rendered from the vendor's own file -----------
        source = payloads["chandra_prompts.py"].decode("utf-8")
        tree = ast.parse(source, filename="chandra/prompts.py")
        assert not [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ], "chandra/prompts.py has gained an import; it is no longer safe to render in place"
        # Executed, not imported, and only after the digest above proved the
        # bytes are the pinned ones and the walk above proved the module has no
        # imports: what runs is four assignments in a namespace of its own.
        namespace: dict[str, Any] = {}
        exec(compile(tree, "chandra/prompts.py", "exec"), namespace)
        rendered = namespace["OCR_LAYOUT_PROMPT"]
        assert _digest(rendered) == CHANDRA_OCR_LAYOUT_PROMPT_SHA256
        assert len(rendered.encode("utf-8")) == CHANDRA_OCR_LAYOUT_PROMPT_BYTES
        if not _CHANDRA_LAYOUT_PENDING:
            module = importlib.import_module("common.chandra_layout")
            assert rendered in _module_string_constants(module).values(), (
                "the prompt this repository carries is not byte-equal to the one "
                "chandra/prompts.py renders at the pinned commit"
            )

        # --- Churro's profile registration, by AST rather than by import -----
        presets = ast.parse(payloads["churro_presets.py"].decode("utf-8"), filename="presets.py")
        specs = ast.parse(payloads["churro_specs.py"].decode("utf-8"), filename="specs.py")
        assert _assigned_literal(presets, "CHURRO_3B_MODEL_ID") == CHURRO_MODEL_ID
        template = _assigned_call(presets, "CHURRO_3B_XML_TEMPLATE")
        keywords = {keyword.arg: keyword.value for keyword in template.keywords}
        assert isinstance(keywords["system_message"], ast.Constant)
        assert keywords["system_message"].value == CHURRO_SYSTEM_MESSAGES["registry-v0.3.0"]
        assert isinstance(keywords["user_prompt"], ast.Constant)
        assert keywords["user_prompt"].value is None, (
            "the vendor registry now gives churro-3B a user prompt; the image-only "
            "user turn this repository sends is no longer the vendor's answer"
        )
        profile = _function_call_keywords(specs, "churro_3b_profile")
        assert _name_of(profile["profile_name"]) == "CHURRO_3B_MODEL_ID"
        assert _name_of(profile["template"]) == "CHURRO_3B_XML_TEMPLATE"
        assert "transport" not in profile and "huggingface" not in profile, (
            "the vendor now overrides transport or generation for churro-3B; the "
            "no-max_tokens, no-temperature reading is no longer its own"
        )
        assert "churro_3b_profile()" in payloads["churro_specs.py"].decode("utf-8"), (
            "churro_3b_profile is no longer built into the profile registry"
        )

        # --- The paper-era arm, from the paper-era file ----------------------
        finetuned = ast.parse(
            payloads["churro_finetuned_ocr.py"].decode("utf-8"),
            filename="ocr/systems/finetuned_ocr.py",
        )
        assert (
            _assigned_literal(finetuned, "SYSTEM_MESSAGE")
            == CHURRO_SYSTEM_MESSAGES["paper-harness-ed09bc7"]
        ), (
            "the paper-era harness's SYSTEM_MESSAGE is no longer the typo'd string "
            "this repository records as arm A4b; the variant table is stating a "
            "provenance the vendor does not have"
        )

        if not _CHURRO_DOCUMENT_PENDING:
            module = importlib.import_module("common.churro_document")
            carried = set(_module_string_constants(module).values())
            for variant, message in CHURRO_SYSTEM_MESSAGES.items():
                assert message in carried, (
                    f"the {variant!r} Churro system message this repository carries is "
                    "not byte-equal to the vendor's own at the commit it names"
                )

        # --- DAI's three carried files, byte for byte ------------------------
        assert (
            payloads["dai_system.txt"].decode("utf-8") == _feeding_literal("dai_prompt")["system"]
        )
        assert payloads["dai_query.txt"].decode("utf-8") == _feeding_literal("dai_prompt")["user"]
        assert json.loads(payloads["dai_generation_config.json"]) == _feeding_literal(
            "dai_generation"
        )

        # --- Both resize ports against the vendors' own functions ------------
        table = [list(entry[0]) for entry in RESIZE_TABLE]
        completed = subprocess.run(
            [sys.executable, "-c", _VENDOR_RESIZE_SUBPROCESS, str(root), json.dumps(table)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        vendor_results = json.loads(completed.stdout)
        assert len(vendor_results) == len(RESIZE_TABLE)
        for (source_size, chandra_expected, churro_expected), (
            vendor_chandra,
            vendor_churro,
        ) in zip(RESIZE_TABLE, vendor_results, strict=True):
            assert tuple(vendor_chandra) == chandra_expected == scale_to_fit_chandra(*source_size)
            assert tuple(vendor_churro) == churro_expected == resize_to_fit_churro(*source_size)


def _assigned_literal(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"the vendor module no longer assigns {name}")


def _assigned_call(tree: ast.Module, name: str) -> ast.Call:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            assert isinstance(node.value, ast.Call)
            return node.value
    raise AssertionError(f"the vendor module no longer assigns {name}")


def _function_call_keywords(tree: ast.Module, function_name: str) -> dict[str, ast.expr]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Call):
                    return {
                        keyword.arg: keyword.value
                        for keyword in child.value.keywords
                        if keyword.arg is not None
                    }
    raise AssertionError(f"the vendor module no longer defines {function_name}")


def _name_of(node: ast.expr) -> str:
    return node.id if isinstance(node, ast.Name) else ast.dump(node)
