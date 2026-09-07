"""A shared fake serving endpoint for stage tests built against :class:`ChairClient`.

Mirrors of :mod:`operations.serving.test_manager`'s own fakes (``FakeHttp``,
``FakeLauncher``, ``FakeProcess``, ``FakePackages``, ``FakeRegistry``) — not
moved from there, so that 4,500-line manager-lifecycle suite stays untouched.
Attestatores and Perlector stage tests both need one scripted endpoint that
speaks the reading contract; deduplicating the two families of fakes is a
named follow-on, not a job this module does.

Beside the reading answers, the builders under "the structure chair's answers"
script what the Designator's `designator_structure` chair returns in Chandra's
own layout-HTML grammar: a page's blocks given in page pixels, a body the
grammar refuses by a named outcome, or a real answer the engine cut off
mid-block. They live here rather than in a suite because knowing which
normalized box lands on a given page rectangle means inverting
`common.structure_answer.to_page_bounds`, and a second copy of that inversion
could agree with a converter that had changed underneath it.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common import chandra_layout, structure_answer
from common.chairs.errors import ServingRecipeRefusal
from common.chairs.models import ChairIdentity, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt

from .client import ChairClient, RetainBytes
from .http import EndpointUnavailable, HttpResponse
from .manager import AdapterCalibration, ReceiptPublication, ServingManager


class _Absent:
    """The sentinel for a ``finish_reason`` key omitted from the wire entirely.

    Distinct from ``None``: passing ``None`` scripts an explicit JSON
    ``null``, while ``ABSENT`` scripts a response whose ``choices[0]`` carries
    no ``finish_reason`` key at all. :func:`operations.serving.http._finish_reason`
    treats both the same way (verbatim absence, never a default) — the fake
    lets one test prove that even though the two wire shapes differ.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "ABSENT"


ABSENT: Any = _Absent()


@dataclass(frozen=True, slots=True)
class ScriptedAnswer:
    """One scripted reply for the next reading POST the fake endpoint receives.

    ``body``, when given, overrides ``content``/``finish_reason``/``usage``/
    ``model`` entirely and is returned as the raw response bytes verbatim —
    the shape a malformed-body test needs. Otherwise the fake builds one
    chat-completions choice from the other fields.
    """

    content: str | None = None
    finish_reason: Any = ABSENT
    usage: Mapping[str, int] | None = None
    model: str | None = None
    status: int = 200
    body: bytes | None = None


class FakeBlobStore:
    """A minimal content-addressed store: the client's ``retain`` and the
    fake endpoint's response-as-arrival check both point at one instance."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.written: list[bytes] = []

    def retain(self, data: bytes) -> dict[str, str]:
        sha256 = hashlib.sha256(data).hexdigest()
        directory = self.root / "blobs" / "sha256"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sha256}.bin"
        path.write_bytes(data)
        self.written.append(data)
        return {"relative_path": f"blobs/sha256/{sha256}.bin", "sha256": sha256}

    def has(self, sha256: str) -> bool:
        """True only when the exact digest named is already on disk."""

        return (self.root / "blobs" / "sha256" / f"{sha256}.bin").exists()

    def __len__(self) -> int:
        return len(self.written)


class FakeProcess:
    """A loopback-process shape only; no real subprocess is ever created."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit_code = -9

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        self.wait_calls += 1
        if self.exit_code is None:
            raise TimeoutError("fake child is still live")
        return self.exit_code

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        del maximum_bytes
        return ""


class FakeEndpoint:
    """A scripted OpenAI-compatible loopback endpoint.

    Health and ``/models`` always answer ready, advertising ``served_model_id``.
    A manager's readiness probe is exactly one POST, made once inside
    ``ServingManager.start`` before any :class:`~operations.serving.client.ChairClient`
    reading is possible; this fake auto-answers that first POST (never
    consuming a scripted answer, never recorded in ``requests``) and treats
    every POST after it as a reading. Each of those pops the next
    :class:`ScriptedAnswer`, in order, and records the decoded request body in
    ``requests`` before it answers.
    """

    def __init__(
        self,
        *,
        served_model_id: str,
        blob_store: FakeBlobStore | None = None,
        assert_retained_before_next_request: bool = False,
        sticky_after_stop: bool = False,
    ) -> None:
        self.served_model_id = served_model_id
        self.blob_store = blob_store
        # A stopped process whose endpoint keeps answering — the exact
        # ambiguity `ServingManager._assert_endpoint_absent` exists to catch
        # (mirrors test_manager.py's own fake). Set true only where a test
        # needs `ChairClient.__enter__`'s own `handle.stop()` to fail.
        self.sticky_after_stop = sticky_after_stop
        # Deliberately opt-in rather than a blanket invariant, because a test
        # may drive this endpoint through a seam that never reaches
        # `ChairClient.read` at all. Every reading that does reach it retains,
        # including a non-200 or wrong-model one: retention now runs before the
        # wrong-source refusal, so vLLM's own account of why it refused is on
        # disk before the refusal is raised.
        self.assert_retained_before_next_request = assert_retained_before_next_request
        self._answers: list[ScriptedAnswer] = []
        self.requests: list[dict[str, object]] = []
        self._process: FakeProcess | None = None
        self._readiness_probe_answered = False
        # The exact raw body this fake last served as a reading answer — not
        # merely a count, so the check below can name the one blob that must
        # already be retained, not just how many blobs exist in total.
        self._last_served_reading_sha256: str | None = None

    def script(self, *answers: ScriptedAnswer) -> None:
        self._answers.extend(answers)

    def bind(self, process: FakeProcess) -> None:
        self._process = process

    def _available(self) -> bool:
        return self._process is not None and (
            self._process.poll() is None or self.sticky_after_stop
        )

    def request(
        self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
    ) -> HttpResponse:
        del timeout_seconds
        if not self._available():
            # Before launch and after a verified stop, no listener owns this
            # loopback port — the exact TCP fact `_assert_endpoint_unoccupied`
            # and `_assert_endpoint_absent` both require to proceed.
            raise EndpointUnavailable(
                f"fake endpoint unavailable at {url}", definitively_absent=True
            )
        if url.endswith("/health"):
            return HttpResponse(200, b'{"status":"ok"}')
        if url.endswith("/models"):
            return HttpResponse(200, json.dumps({"data": [{"id": self.served_model_id}]}).encode())
        if method != "POST":
            return HttpResponse(404, b"{}")
        decoded = json.loads(body) if body is not None else None
        if not self._readiness_probe_answered:
            # `ServingManager.start` makes exactly one such POST, always
            # before a `ChairClient` can issue its first reading. Readiness
            # itself is proven elsewhere (operations/serving/test_manager.py);
            # this fake only needs it to succeed, and it must never consume a
            # scripted reading answer or pollute the reading-call count.
            self._readiness_probe_answered = True
            return self._auto_probe_response(decoded, url)
        if (
            self._last_served_reading_sha256 is not None
            and self.assert_retained_before_next_request
            and self.blob_store is not None
        ):
            # Response-as-arrival: the *exact* bytes this fake served as the
            # previous reading must already be on disk, by their own digest,
            # before this next reading request ever reaches the endpoint. A
            # blob count alone would be satisfied by any retention order (the
            # client also retains a call-record blob per read); naming the
            # digest is what actually pins retain-before-parse.
            if not self.blob_store.has(self._last_served_reading_sha256):
                raise AssertionError(
                    "the prior reading's raw response "
                    f"(sha256={self._last_served_reading_sha256}) was not retained "
                    "before the next reading request was sent"
                )
        self.requests.append(decoded)
        answer = self._answers.pop(0)
        if answer.body is not None:
            body = answer.body
        else:
            choice: dict[str, object] = {"message": {"content": answer.content}}
            if answer.finish_reason is not ABSENT:
                choice["finish_reason"] = answer.finish_reason
            payload: dict[str, object] = {
                "model": answer.model if answer.model is not None else self.served_model_id,
                "choices": [choice],
            }
            if answer.usage is not None:
                payload["usage"] = dict(answer.usage)
            body = json.dumps(payload).encode()
        # Every body this fake serves as a reading is retained by
        # `ChairClient.read`, whatever its status and whatever model it names:
        # retention happens before the wrong-source check, so a 400 explaining
        # a context overflow reaches disk before it is refused. The fake
        # therefore predicts retention for all of them.
        self._last_served_reading_sha256 = hashlib.sha256(body).hexdigest()
        return HttpResponse(answer.status, body)

    def _auto_probe_response(self, decoded: dict[str, object] | None, url: str) -> HttpResponse:
        model_id = decoded.get("model") if isinstance(decoded, dict) else None
        if url.endswith("/chat/completions"):
            choice: dict[str, object] = {"message": {"content": "ready"}}
        else:
            choice = {"text": "ready"}
        return HttpResponse(
            200,
            json.dumps({"model": model_id or self.served_model_id, "choices": [choice]}).encode(),
        )


# --------------------------- the structure chair's answers ---------------------------
#
# SPEC_D §5. A test that hand-writes the structure chair's answer has to spell
# the normalized geometry itself, and the only way to know which box lands on a
# given page rectangle is to invert `structure_answer.to_page_bounds` — so
# every suite that scripts the chair would carry its own copy of that
# inversion, and each copy could drift from the converter it is inverting. The
# builders below do it once, and prove it each time by running the answer they
# built back through the grammar that will parse it.
#
# **The grammar is Chandra's own layout HTML** since the structure chair moved
# onto the vendor's prompt bytes (Tyrel, 2026-09-06): top-level `<div>` blocks
# carrying `data-bbox="x0 y0 x1 y1"` normalized 0–1000 and `data-label` from the
# prompt's own nineteen. These builders emit exactly that, and check it back
# through `common/chandra_layout.py::parse_layout_html` and
# `block_page_bounds` — never through a second formula of their own.


def structure_box_1000(bounds: Mapping[str, int], page_w: int, page_h: int) -> list[int]:
    """The normalized `box_1000` whose page-pixel conversion is exactly ``bounds``.

    Found by search over the 0–1000 grid and checked through
    :func:`common.structure_answer.to_page_bounds` itself, never by a second
    closed-form formula: a builder that re-derived the arithmetic could agree
    with a converter that had changed underneath it, which is precisely the
    coordinate-space confusion the normalized contract exists to prevent.

    Refuses by name when no box converts to the rectangle asked for — the
    quantized grid is coarser than the page for small pages, and a silently
    approximated rectangle would make a test's minted `raw_bounds` a near miss
    nobody declared.
    """

    def _low(page: int, target: int) -> int:
        for value in range(1001):
            if value * page // 1000 == target:
                return value
        raise ValueError(f"no normalized low edge converts to {target} on a page of {page}")

    def _far(page: int, target: int) -> int:
        for value in range(1001):
            if min(page - 1, (value * page + 999) // 1000 - 1) == target:
                return value
        raise ValueError(f"no normalized far edge converts to {target} on a page of {page}")

    box = [
        _low(page_w, bounds["x"]),
        _low(page_h, bounds["y"]),
        _far(page_w, bounds["x"] + bounds["w"] - 1),
        _far(page_h, bounds["y"] + bounds["h"] - 1),
    ]
    converted = structure_answer.to_page_bounds(box, page_w, page_h)
    if converted != dict(bounds):
        raise ValueError(f"box {box} converts to {converted}, not to {dict(bounds)}")
    return box


def structure_layout_block(
    bounds: Mapping[str, int] | None,
    text: str,
    page_w: int,
    page_h: int,
    *,
    label: str | None = "Text",
    bbox_attribute: str | None = None,
) -> str:
    """One top-level layout `<div>` in Chandra's grammar.

    ``bounds`` is in page pixels and is inverted to the normalized box that
    converts back to exactly it. ``bbox_attribute`` overrides that with a raw
    attribute value, which is how a test scripts the malformed-bbox block the
    pass retains and never mints; ``bounds=None`` with no override writes a
    block carrying no `data-bbox` at all. ``label=None`` writes no `data-label`,
    which is the vendor's "no label declared" case.
    """
    attributes = ""
    if bbox_attribute is not None:
        attributes += f' data-bbox="{html.escape(bbox_attribute, quote=True)}"'
    elif bounds is not None:
        box = structure_box_1000(bounds, page_w, page_h)
        attributes += f' data-bbox="{box[0]} {box[1]} {box[2]} {box[3]}"'
    if label is not None:
        attributes += f' data-label="{html.escape(label, quote=True)}"'
    return f"<div{attributes}>{html.escape(text)}</div>"


def structure_answer_body(
    acts: Sequence[tuple[Mapping[str, int], str] | tuple[Mapping[str, int], str, str]],
    page_w: int,
    page_h: int,
) -> str:
    """The structure chair's layout HTML for rectangles given in page pixels.

    Each act is ``(bounds, text)`` — a block carrying no `data-label` at all,
    which is the vendor's "no label declared" case and the one that makes
    `label_declared` false and both label fields null on the Designator's record
    — or ``(bounds, text, label)`` to set the block's `data-label`.

    An empty sequence is the chair's "I see no text" answer. It is written as
    one `Blank-Page` block rather than as an empty
    string, because an empty string is not a layout answer at all — the grammar
    refuses it as `no-layout-blocks`, and `Blank-Page` is how Chandra's own
    prompt tells it to say the page is empty. That is the page-fallback row of
    SPEC_D §1.4.

    The `Blank-Page` block is written **with** a whole-page `data-bbox`,
    because the vendor prompt asks for one on every layout block and a real
    answer would carry it. It changes nothing about the page's outcome — the
    grammar gives a `Blank-Page` block no page rectangle whatever its declared
    box — and it keeps the fixture from also scripting a missing attribute
    nobody meant to test.
    """
    if not acts:
        return structure_layout_block(
            None,
            "",
            page_w,
            page_h,
            label=chandra_layout.BLANK_PAGE_LABEL,
            bbox_attribute=f"0 0 {chandra_layout.BBOX_SCALE} {chandra_layout.BBOX_SCALE}",
        )
    return "\n".join(
        structure_layout_block(
            act[0], act[1], page_w, page_h, label=act[2] if len(act) > 2 else None
        )
        for act in acts
    )


def scripted_structure_answer(
    acts: Sequence[tuple[Mapping[str, int], str] | tuple[Mapping[str, int], str, str]],
    page_w: int,
    page_h: int,
    *,
    finish_reason: Any = "stop",
    **fields: Any,
) -> ScriptedAnswer:
    """One page's scripted structure answer, verified against the grammar.

    The body is parsed here, before any test sees it, so a builder that drifted
    from `common/chandra_layout.py` fails in the builder rather than as an
    unexplained hold three stages downstream. Both halves of the round trip are
    checked — each block's page rectangle and each block's text under the
    declared text view — because a builder that escaped its text wrongly would
    otherwise script a body whose digests silently differ from what it meant.
    ``finish_reason="length"`` scripts the cut-off row of SPEC_D §1.4 over a
    body that nonetheless parses.
    """
    content = structure_answer_body(acts, page_w, page_h)
    parsed = chandra_layout.parse_layout_html(content.encode())
    if chandra_layout.is_refusal(parsed):
        raise ValueError(f"the scripted answer does not parse: {parsed['parse_outcome']}")
    blocks = parsed["blocks"]
    # The zero-act answer is one `Blank-Page` block, which by the grammar's own
    # rule has no page rectangle and no text -- so what it must round-trip to
    # is exactly that, not an empty block list.
    expected = [(None, "")] if not acts else [(dict(act[0]), act[1]) for act in acts]
    placed = [
        chandra_layout.block_page_bounds(block, page_size=(page_w, page_h)) for block in blocks
    ]
    if placed != [bounds for bounds, _text in expected]:
        raise ValueError("the scripted answer's rectangles do not survive the round trip")
    if [block["text"] for block in blocks] != [text for _bounds, text in expected]:
        raise ValueError("the scripted answer's text does not survive the round trip")
    return ScriptedAnswer(content=content, finish_reason=finish_reason, **fields)


# One body per named refusal, each the smallest answer that reaches that
# outcome and nothing else. Keyed by the `PARSE_OUTCOMES` code so a test names
# the outcome it is scripting rather than a body it has to be read to decode.
#
# **Three of the grammar's six outcomes are scriptable here and three are not,
# and that is a property of this seam rather than a gap in coverage.** A
# `ScriptedAnswer` carries a `str` the pass encodes as UTF-8, so
# `raw-response-not-bytes` and `invalid-utf8` cannot be reached through an
# endpoint at all, and `response-too-large` needs a body above
# `chandra_layout.MAX_RESPONSE_BYTES` (16 MiB) — three orders of magnitude more
# than any fixture page, and nothing a wire fake should be materialising.
# `common/test_chandra_layout.py` reaches all three directly on bytes.
_STRUCTURE_REFUSALS: Mapping[str, str] = {
    "no-layout-blocks": (
        "# Page one\n\nMarkdown the chair wrote instead of the layout blocks it was asked for."
    ),
    "blocks-not-at-top-level": (
        '<article><div data-bbox="10 10 900 900" data-label="Text">wrapped</div></article>'
    ),
    "too-many-layout-blocks": "\n".join(
        f'<div data-bbox="1 1 2 2" data-label="Text">b{index}</div>'
        for index in range(chandra_layout.MAX_LAYOUT_BLOCKS + 1)
    ),
}


def scripted_structure_refusal(
    outcome: str, *, finish_reason: Any = "stop", **fields: Any
) -> ScriptedAnswer:
    """An answer the layout grammar refuses, by the exact outcome named.

    Verified through `chandra_layout.parse_layout_html`, which takes no page
    size at all, so the outcome is a property of the body and cannot depend on
    a page the caller happens to be using.
    """
    if outcome not in _STRUCTURE_REFUSALS:
        raise ValueError(
            f"no scripted body refuses as {outcome!r}; "
            f"the ones built here are {sorted(_STRUCTURE_REFUSALS)}"
        )
    content = _STRUCTURE_REFUSALS[outcome]
    parsed = chandra_layout.parse_layout_html(content.encode())
    if parsed.get("parse_outcome") != outcome:
        raise ValueError(
            f"the scripted body refuses as {parsed.get('parse_outcome')!r}, not {outcome!r}"
        )
    return ScriptedAnswer(content=content, finish_reason=finish_reason, **fields)


def scripted_prompt_too_long(
    *,
    max_model_len: int,
    requested_tokens: int,
    prompt_tokens: int,
    completion_tokens: int = 0,
    model: str | None = None,
) -> ScriptedAnswer:
    """The refusal vLLM actually gives when the **prompt** exceeds the context.

    This is the other half of the failure `scripted_structure_cut_off` scripts,
    and it is the half that stops the first real page. That one is a
    ``finish_reason="length"`` inside an HTTP 200: the engine generated, and
    generation ran out of room. This one is an HTTP **400** with no choices at
    all: the engine refused before it generated, because the request could not
    be admitted. A stage that reads the first as "the answer was cut off" would
    read the second the same way and record a truncated reading where no
    reading exists, so both must be scripted and both must be exercised.

    The body is vLLM's own OpenAI-compatible error envelope
    (``{"object": "error", "message": ..., "type": "BadRequestError", "param":
    null, "code": 400}``) carrying the sentence its context check emits. Its
    exact wording has never been observed from a live engine by this
    repository -- only its *shape* is asserted here, and what the stages are
    proven to do with it is refuse by name and retain it, never parse it.

    ``model`` is deliberately absent by default: vLLM's error envelope names no
    model, and a fake that added one would let the client's wrong-source check
    fire on the wrong code.
    """

    message = (
        f"This model's maximum context length is {max_model_len} tokens. "
        f"However, you requested {requested_tokens} tokens "
        f"({prompt_tokens} in the messages, {completion_tokens} in the completion). "
        "Please reduce the length of the messages or completion."
    )
    payload: dict[str, Any] = {
        "object": "error",
        "message": message,
        "type": "BadRequestError",
        "param": None,
        "code": 400,
    }
    if model is not None:
        payload["model"] = model
    return ScriptedAnswer(status=400, body=json.dumps(payload).encode())


def scripted_structure_cut_off(
    acts: Sequence[tuple[Mapping[str, int], str] | tuple[Mapping[str, int], str, str]],
    page_w: int,
    page_h: int,
    **fields: Any,
) -> ScriptedAnswer:
    """A whole-page answer the engine stopped mid-block, as a real overrun looks.

    The body is the complete answer truncated inside its first block's text,
    and the stop word is `"length"`.

    **Under the layout grammar the stop word is the whole signal, and that is
    the point of this fake.** A truncated JSON object was invalid JSON, so the
    old cut-off body failed to parse and the hold could have come from either
    check. HTML degrades instead: `html.parser` reads the truncated block, the
    grammar returns it with an `unclosed-block` finding, and the page would mint
    a short act list under a clean `parsed` state if nothing else caught it. So
    this builder asserts the truncated body **still parses**, which is what
    makes the test over it a test of `_finish_reason_disposition` running
    before the parse outcome rather than a coincidence.

    **This is the answer-side truncation, and it is real** -- an engine that
    admits a request and then runs out of room to finish it stops exactly like
    this. It is *not* the failure SPEC_D §7 names as the likely first real one:
    a `max_model_len` too small for a page is refused before generation with an
    HTTP 400 and no choices at all, which is `scripted_prompt_too_long`.
    """
    if not acts:
        raise ValueError("a cut-off answer needs at least one block to be cut off inside")
    whole = structure_answer_body(acts, page_w, page_h)
    opening = whole.index(">") + 1
    cut = whole[: opening + max(1, len(acts[0][1]) // 2)]
    parsed = chandra_layout.parse_layout_html(cut.encode())
    if chandra_layout.is_refusal(parsed):
        raise ValueError(
            f"the truncated body refuses as {parsed['parse_outcome']!r}; this fake exists to "
            "prove the hold comes from the stop word over a body that parses"
        )
    if [finding["kind"] for finding in parsed["findings"]] != ["unclosed-block"]:
        raise ValueError(
            "the truncated body did not leave its last block unclosed, so it does not look "
            "like an engine that ran out of room mid-block"
        )
    return ScriptedAnswer(content=cut, finish_reason="length", **fields)


class FakeLauncher:
    def __init__(self, endpoint: FakeEndpoint) -> None:
        self.endpoint = endpoint
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.processes: list[FakeProcess] = []

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> FakeProcess:
        self.calls.append((argv, log_path))
        process = FakeProcess(9000 + len(self.processes))
        self.processes.append(process)
        self.endpoint.bind(process)
        return process


class FakePackages:
    def __init__(self, versions: Mapping[str, str]) -> None:
        self.versions = dict(versions)

    def version(self, package: str) -> str:
        return self.versions[package]


class FakeRegistry:
    def __init__(self, identities: Mapping[str, ChairIdentity], tmp_path: Path) -> None:
        self.identities = dict(identities)
        self.snapshots = {
            role: VerifiedSnapshot(chair_identity, tmp_path / role, chair_identity.digest_manifest)
            for role, chair_identity in identities.items()
        }

    def resolve(self, role: str) -> ChairIdentity:
        return self.identities[role]

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        return self.snapshots[identity.role]

    def receipt(self, identity: ChairIdentity, details: ServingDetails):
        return build_receipt(identity, details)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        raise ServingRecipeRefusal(identity.role, difference)


class FakePublisher:
    """Publishes a receipt/audit/evidence triple content-addressed by the audit."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Mapping[str, object]]] = []

    def publish(self, receipt: object, launch_audit: Mapping[str, object]) -> ReceiptPublication:
        self.calls.append((receipt, launch_audit))
        digest = hashlib.sha256(
            json.dumps(launch_audit, sort_keys=True, default=str).encode()
        ).hexdigest()
        return ReceiptPublication(
            {"relative_path": f"receipts/sha256/{digest}.json", "sha256": digest},
            {"relative_path": f"stages/blobs/sha256/{digest}-audit", "sha256": digest},
            {"relative_path": f"stages/blobs/sha256/{digest}-evidence", "sha256": digest},
        )


def fake_serving_factory(
    *,
    manager: ServingManager,
    retain: RetainBytes,
    decoding_config_sha256: str,
    read_receipt: Callable[[Mapping[str, str]], Mapping[str, object]],
    record_temperature: int = 0,
    adapter_calibration: AdapterCalibration | None = None,
) -> Callable[[Any, ChairIdentity, str], ChairClient]:
    """Build the ``serving_factory(context, chair, tier) -> ChairClient`` a
    stage's ``main`` calls under live mode, wired to one fake manager.

    ``context`` is accepted and ignored: production factories close over a
    real ``StageContext`` to build ``retain``/``read_receipt``, but this fake
    factory already has both, supplied directly by the test.
    """

    def factory(context: object, identity: ChairIdentity, tier: str) -> ChairClient:
        del context
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=retain,
            decoding_config_sha256=decoding_config_sha256,
            record_temperature=record_temperature,
            read_receipt=read_receipt,
            adapter_calibration=adapter_calibration,
        )

    return factory
