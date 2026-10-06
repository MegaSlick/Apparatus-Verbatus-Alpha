# Brief 0037: make the defaults pin hold on every platform

- Role: build side.
- Issued by the host session at 2026-10-04T16:53:52Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  411b6757aa638b12f4054476a8a3f07f01f3eec5f22fc9c00a4ef1b28fdd3c9f
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Sixth follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules. First merge work/pagekit-prepare. CI fails on the Apple-silicon Mac job (macos-15, Python 3.12) in one test: pagekit/test_defaults_pinned.py::test_defaults_give_the_pages_and_manifest_values_of_before, with "manifest.pages[0].source.sha256: '2e4e0ce0…' != '8728c308…'". The Linux and other jobs pass. The test builds its synthetic source images at run time, and their encoded bytes differ by platform (the image libraries' compressors differ), so a pinned file hash of a source cannot hold everywhere. The same may be true of pinned output file hashes, since pagekit's TIFF writer compresses with zlib, whose output can differ between zlib builds; pixel hashes would not.

Fix:
1. Make the pin test platform-independent: generate its sources so their bytes do not depend on a compressor (for example uncompressed TIFF or PNG written by pagekit's own deterministic writer with no compression, or raw pixels saved as test data), and compare decoded-pixel hashes, not file hashes, for both sources and outputs; keep every non-hash manifest value pinned exactly. Regenerate the pinned data from the code before spec 0007 (commit 2d10a14 or earlier) so the pin still proves that defaults did not change.
2. Look for any other test that pins an encoded file hash across processes or platforms and say whether it could differ on another zlib or libjpeg build; where it could, compare pixels instead or document why bytes are expected to match.
3. Say in the README what is byte-identical (same platform and library versions) and what is identical everywhere (decoded pixels and manifest values).

Run the pagekit suite under the check script's settings. Report the head.
