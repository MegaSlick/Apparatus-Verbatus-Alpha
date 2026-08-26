"""Small shared limits and field names for the confined ingest protocol."""

from typing import Final

# The instrument suite's explicit real-corpus-order case is 1,200 frames and
# 14,322 adjacency candidates.  These ceilings leave working margin above that
# case while refusing before the inventory's general 100,000-file allowance can
# become thousands of retained proxies or an unbounded full-comparison list.
MAX_INGEST_FRAMES: Final = 1_500
MAX_INGEST_CANDIDATE_PAIRS: Final = 20_000
# A corpus id is copied into every produced row.  This is far above an ordinary
# identifier and prevents one command-line value from multiplying across a full
# corpus manifest.
MAX_CORPUS_ID_CHARACTERS: Final = 256
# Presentation lines cross from the confined child to the parent and terminal.
# Match the operator error detail ceiling so neither route treats a longer line
# as a complete, displayable fact.
MAX_PREVIEW_LINE_CHARACTERS: Final = 2_000

EXPECTED_DIGEST_FIELDS: Final = (
    "expected_submission_manifest_sha256",
    "expected_confirmation_sha256",
    "expected_instrument_config_sha256",
    "expected_data_handling_policy_sha256",
)
EXPECTED_OUTPUT_IDENTITY_FIELDS: Final = (
    "expected_output_device",
    "expected_output_inode",
)
REQUEST_FIELDS: Final = frozenset(
    {
        "operation",
        "source",
        "output_dir",
        "policy",
        "corpus_id",
        "mode",
        "confirmation_file",
        *EXPECTED_DIGEST_FIELDS,
        *EXPECTED_OUTPUT_IDENTITY_FIELDS,
    }
)
