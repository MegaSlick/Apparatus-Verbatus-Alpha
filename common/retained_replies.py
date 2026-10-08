"""Which retained chair replies in a stage's store no record of that stage binds.

A live chair call retains its reply and its call record in the calling stage's blob
store before any record names them, so a pass stopped in between leaves bytes no
record binds. A resumed pass reads that here before it asks again, so a reply is
never asked for twice without its first answer on record.
"""

import json
from typing import Any, Final

from common.contracts.serving import (
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
    CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    SERVING_LAUNCH_AUDIT_SCHEMA,
)
from common.image_sniff import PNG_SIGNATURE

# The call records a chair call leaves, whole or streamed.
CALL_RECORD_SCHEMAS: Final = frozenset(
    {
        CHAIR_CALL_RECORD_SCHEMA,
        CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
        CHAIR_STREAM_CALL_RECORD_SCHEMA,
        CHAIR_STREAM_TRANSPORT_FAILURE_RECORD_SCHEMA,
    }
)
# Blobs the serving manager keeps in a stage's own store beside the chair's calls.
SERVING_BLOB_SCHEMAS: Final = frozenset({SERVING_LAUNCH_AUDIT_SCHEMA, "serving-evidence.v1"})


def json_object(data: bytes) -> dict[str, Any] | None:
    """The bytes as a JSON object, or `None` when they are not one."""
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def unrecorded_replies(context, stage: str) -> tuple[list[dict[str, Any]], bool]:
    """Every retained reply no record of `stage` binds, as far as it can be attributed.

    Returns the unbound call records that carry a reply, and whether any retained blob is
    a reply that cannot be attributed at all. The client retains a reply's raw bytes
    before the call record that names them, so a pass stopped between the two leaves
    bytes no call record names: every blob that is not a call record, a reply one names,
    serving evidence, a page render, or an input of some record is counted as such a
    reply, and so is a call record of another schema, since nothing here can read it.
    """
    manifest = context.tree.build_manifest(stage)
    bound = {
        reference["relative_path"]
        for entry in manifest["artifacts"]
        for reference in context.tree.read_artifact(stage, entry["kind"], entry["artifact_id"])[
            "inputs"
        ]
    }
    calls, named, others = [], set(), []
    for name in manifest["blobs"]:
        path = context.tree.blob_path(stage, name)
        data = context.tree.read_bytes(path)
        if data.startswith(PNG_SIGNATURE):
            # A page render the stage cut for a reader call; a chat endpoint's reply is
            # never an image.
            continue
        record = json_object(data)
        schema = record.get("schema") if record is not None else None
        if schema in CALL_RECORD_SCHEMAS:
            reply = record.get("raw_response_ref")
            if reply is not None:
                named.add(reply["relative_path"])
                if path not in bound:
                    calls.append(record)
        elif schema not in SERVING_BLOB_SCHEMAS:
            others.append(path)
    unattributed = any(path not in bound and path not in named for path in others)
    return calls, unattributed
