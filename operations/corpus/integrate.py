"""Closes the U2/U3 seam: a sealed fetch log becomes `submission.FetchedPage` objects.

`submission.py`'s `FetchedPage` docstring is explicit about a boundary it keeps on
purpose: "nothing here reads `private/corpora/recordgold/cache/` directly, so no
coupling to U2's internal layout is baked in." U3 was built before U2 existed, and
that boundary is why it still holds today — `submission.build_submission` takes
`fetched_pages: dict[str, FetchedPage]` as a plain argument and never once opens
`cache/`. Reading the cache is exactly what this module exists to do instead:
`fetched_pages_from_log` is the one place U2's on-disk layout
(`cache.body_path` — `cache/<response-sha256>.jpg`) and U3's `FetchedPage` contract
meet. That coupling has to live *somewhere*; putting it in a third module rather
than folding it into `submission.py` keeps U3's own file honest about the claim
its docstring already makes, and keeps this seam small enough that a change to
either U2's log shape or U3's `FetchedPage` shape touches one file, not both of
theirs.

**Rule 6, applied at this exact boundary.** A fetch log is revalidated here via
`fetch.validate_fetch_log` — the closed-shape check that, as of this seam closing,
also refuses a malformed `"fetched"` entry by name rather than letting a caller
hit a `KeyError` three lines later. Past that, two more facts are checked before a
`FetchedPage` is ever handed out: the cache file the log names actually exists,
and it hashes to the digest the log declares. Neither is optional — a log is not
proof a file is still there, or that it was never touched, only a claim about what
was true at seal time.
"""

from pathlib import Path
from typing import Any

from common.contracts.canonical import digest_bytes

from . import CorpusRefusal
from . import cache as cache_module
from .fetch import validate_fetch_log
from .submission import FetchedPage

INTEGRATE_REFUSAL_REASONS = frozenset(
    {
        "fetched-page-cache-missing",
        "fetched-page-cache-digest-mismatch",
    }
)


def fetched_pages_from_log(log: dict[str, Any], cache_root: Path) -> dict[str, FetchedPage]:
    """Every `"fetched"` entry in `log`, turned into a verified `FetchedPage`.

    `log` is revalidated (never trusted merely because a caller already ran it
    through `validate_fetch_log` once — same discipline `build_submission` already
    applies to `plan`/`snapshot`/`holdout`). A `"refused"` or `"halted"` entry
    contributes nothing to the returned mapping: `build_submission` already
    refuses `page-not-fetched` by name for an identifier with no `FetchedPage`, so
    skipping those entries here is what makes that refusal actually reachable
    rather than duplicating it.

    Refuses `fetched-page-cache-missing` if `cache/<response-sha256>.jpg` is not
    on disk, and `fetched-page-cache-digest-mismatch` if it is there but hashes to
    something other than the digest the log itself declares — the log is a claim
    about bytes that existed at seal time, not a guarantee about the file today.
    """
    log = validate_fetch_log(log)
    cache_root = Path(cache_root)
    fetched_pages: dict[str, FetchedPage] = {}
    for entry in log["entries"]:
        if entry["status"] != "fetched":
            continue
        identifier = entry["identifier"]
        response_sha256 = entry["response_sha256"]
        cache_path = cache_module.body_path(cache_root, response_sha256)
        if not cache_path.exists():
            raise CorpusRefusal(
                f"fetched-page-cache-missing: {identifier!r} names response digest "
                f"{response_sha256!r} but {cache_path} does not exist"
            )
        actual_digest = digest_bytes(cache_path.read_bytes())
        if actual_digest != response_sha256:
            raise CorpusRefusal(
                f"fetched-page-cache-digest-mismatch: {identifier!r} cache file {cache_path} "
                f"digests to {actual_digest!r}, the fetch log declared {response_sha256!r}"
            )
        fetched_pages[identifier] = FetchedPage(
            cache_path=cache_path,
            info_url=entry["info_url"],
            image_url=entry["image_url"],
            size_parameter=entry["size_parameter_used"],
            response_sha256=response_sha256,
            bytes=entry["bytes"],
            http_status=entry["http_status"],
            fetched_at_utc=entry["fetched_at_utc"],
            declared_width=entry["declared_width"],
            declared_height=entry["declared_height"],
            width=entry["width"],
            height=entry["height"],
        )
    return fetched_pages
