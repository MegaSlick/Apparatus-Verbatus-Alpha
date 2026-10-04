# Brief 0032: skip an unusable source with a flag

- Role: build side.
- Issued by the host session at 2026-10-04T15:34:06Z, as a follow-up message to the build-side agent of the named brief.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  9001e468ff317016a552d8166fdb0c7b0eb105b6f05a5b5dea4f187ee022a0c3
- Saved by the host before the message was sent.

----- brief below this line, exactly as sent -----
Fourth follow-up to brief 0023, same worktree (/home/user/verbatus-worktrees/pk-integrate, branch work/pk-integrate) and rules. First merge work/pagekit-prepare (it carries the orientation fixes and an updated spec 0002). The lead has decided that one unusable source file must not stop a batch, and the host has changed spec 0002 accordingly: read its "Command line" section and its last list of behaviours the tests must pin. In short: a source that is unreadable, truncated, not an image, or in a mode or depth pagekit does not support is skipped; the other sources are prepared as usual; the skipped file is named with a plain reason in the manifest, at the top of the review sheet (before the list of sources) and in the command's output; it gets no page and no project entry, so a later run tries it again; the exit status is 1. Exit status 2, with nothing written, stays for problems that stop the whole run: no usable source at all, a missing or unwritable output folder, an output folder inside the source folder, an unreadable project or overrides file, or an invalid option.

For each behaviour, add a failing test first, then change the code. Tests: a folder with two good pages, a text file named .png, a truncated JPEG, a 16-bit grey page and a CMYK page prepares the two good pages, names the four others with their reasons in all three places, and exits 1; the same folder with only bad files exits 2 and writes nothing; a second run after one bad file is replaced with a good one prepares it and keeps the others' values; the manifest's schema stays closed and its new field is documented. Keep the existing refusals for the folder, project and overrides problems. Run the whole pagekit suite under the check script's settings (env -u PYTHONPATH PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1) and check-static. Report the head and what each test proved.
