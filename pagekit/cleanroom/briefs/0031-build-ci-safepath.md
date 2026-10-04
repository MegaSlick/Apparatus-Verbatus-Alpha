# Brief 0031: tests and the printed command under the check script's settings

- Role: build side.
- Issued by the host session at 2026-10-04T15:11:28Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  9b37693b1c9533cd468d1c7b2fb816e8583ff3819d34e1730b9d906fec5195f1
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Third follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules. CI fails on the pull request that carries pagekit, and the host reproduced it locally: the repository's check script runs every test with PYTHONSAFEPATH=1, PYTHONNOUSERSITE=1, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 and PYTHONPATH unset (see .githooks/check-all.sh lines 29-36). Under those settings a child Python started by a test no longer has the current folder on its import path, so these fail with exit 1 or a wrong result:
- pagekit/test_pipeline.py::test_a_tiff_with_an_odd_strip_is_identical_from_two_processes (all six cases): the child cannot import pagekit.
- pagekit/test_pipeline.py::test_an_override_line_pasted_into_an_overrides_file_changes_exactly_that_step: the printed `cd <folder> && <python> -m pagekit prepare ...` command cannot import pagekit, so the override is not applied ("detected" instead of "manual").

Reproduce with: env -u PYTHONPATH PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q pagekit/test_pipeline.py

Fix:
1. In the tests, start child processes so they import pagekit whatever PYTHONSAFEPATH says (for example, pass PYTHONPATH set to the folder holding pagekit in the child's environment).
2. The correction command printed on the review sheet must work for a person whose shell sets PYTHONSAFEPATH: make it set the import path explicitly (for example `PYTHONPATH=<folder holding pagekit> <python> -m pagekit prepare ...`, shell-quoted, with the Windows form left for later), keep it one line to copy, and keep the README in step. Test it in a subprocess with PYTHONSAFEPATH=1 set.
3. Then run the whole pagekit suite under those same settings, as above, and fix anything else that fails the same way.
Report the head and what each test proved.
