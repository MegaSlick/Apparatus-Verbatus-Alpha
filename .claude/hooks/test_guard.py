"""What the guard must refuse, and — at least as important — what it must not.

The suite this replaced was 2,149 lines and tested a guard with three verdicts. Most
of it pinned the precision of the `ask` path: which of two audiences saw which
sentence, in how many spellings of the same command. None of that survives, because
the ask does not.

The half of this file that matters most is `TestSilence`. A guard is only worth having
if it is quiet, and the failure that retired the last one was not a missed denial — it
was 503 prompts in three days, which taught the reflex that made the 149 real ones
invisible. Every case there is a command that must pass without a word.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("guard.py")
ROOT = SCRIPT.parents[2]
SETTINGS = ROOT / ".claude" / "settings.json"

SPEC = importlib.util.spec_from_file_location("verbatus_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


# ------------------------------------------------------------------- test helpers


def decide(command: str, cwd: Path | str = ROOT):
    return guard.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    )


def decide_write(path: Path | str, content: str = "hello", cwd: Path | str = ROOT):
    return guard.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(path), "content": content},
            "cwd": str(cwd),
        }
    )


def denied(decision) -> bool:
    return bool(decision) and decision[0] == "deny"


def checkout_on(directory: Path, branch: str) -> Path:
    """A directory that looks to the guard like a checkout standing on `branch`."""
    (directory / ".git").mkdir(parents=True, exist_ok=True)
    (directory / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return directory


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway project root, so path checks judge against it rather than the real one."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / "workbench" / "scratch").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def project_under_tmp(monkeypatch):
    """A project root that genuinely lives under `/tmp`.

    What actually sits under `/tmp` in a chamber is pytest's own fixture root — a
    chamber clones to `/work` and GitHub checks out under `/home/runner/work`. But a
    checkout *can* land there, in a scratch clone or a sandbox, and treating the
    prefix as disposable wherever it appeared judged that whole tree disposable.

    `tmp_path` cannot stand in for it. On Linux that fixture already lands under
    `/tmp`, so these cases pass there for the wrong reason; on macOS it lands under
    `/var/folders/…`, which resolves to `/private/var/folders/…` and matches no
    disposable prefix at all. Naming the directory explicitly is what makes the same
    case mean the same thing on both.
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        # One level down, deliberately. A root placed directly under `/tmp` has no
        # intermediate parent, and the first version of these tests could not see the
        # hole all three review seats found: `rm -rf /tmp/<dir>` destroys a checkout
        # at `/tmp/<dir>/repo` without ever being inside it.
        root = Path(directory) / "repo"
        (root / "workbench" / "scratch").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        yield root


# --------------------------------------------------- 1. landing work on `main`


class TestMain:
    def test_a_commit_on_main_is_refused(self, tmp_path):
        assert denied(decide("git commit -m 'work'", checkout_on(tmp_path, "main")))

    def test_a_commit_on_a_work_branch_is_not(self, tmp_path):
        assert decide("git commit -m 'work'", checkout_on(tmp_path, "work/topic")) is None

    def test_a_push_at_main_is_refused(self, tmp_path):
        directory = checkout_on(tmp_path, "work/topic")
        assert denied(decide("git push origin main", directory))
        assert denied(decide("git push origin HEAD:main", directory))
        assert denied(decide("git push origin HEAD:refs/heads/main", directory))

    def test_a_bare_push_from_a_checkout_on_main_is_refused(self, tmp_path):
        # The ordinary, undecorated form. It names no refspec, so the arm that reads
        # refspecs saw nothing and let it through — while the docstring claimed this
        # refusal covered pushing to main. Found by a Sonnet seat running the guard
        # against real payloads inside a chamber.
        directory = checkout_on(tmp_path, "main")
        assert denied(decide("git push", directory))
        assert denied(decide("git push origin", directory))
        assert denied(decide("git push --tags", directory))

    def test_a_bare_push_from_a_work_branch_still_passes(self, tmp_path):
        directory = checkout_on(tmp_path, "work/topic")
        assert decide("git push", directory) is None
        assert decide("git push origin work/topic", directory) is None

    def test_a_write_into_a_checkout_on_main_is_refused(self, tmp_path):
        directory = checkout_on(tmp_path, "main")
        assert denied(decide_write(directory / "notes.md"))

    def test_a_write_into_a_worktree_off_main_is_not(self, tmp_path):
        directory = checkout_on(tmp_path, "work/topic")
        assert decide_write(directory / "notes.md") is None

    def test_a_detached_head_is_not_main(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("9d3f2c1\n", encoding="utf-8")
        assert decide("git commit -m 'work'", tmp_path) is None

    def test_the_refusal_quotes_the_rule_rather_than_naming_a_number(self, tmp_path):
        # A refusal that cites "hard rule 3" and stops tells the reader nothing they
        # can argue with. This one has to be readable on sight.
        decision = decide("git commit -m 'work'", checkout_on(tmp_path, "main"))
        assert "A session never works from `main`" in decision[1]
        assert "git switch -c" in decision[1]

    def test_reading_main_is_not_landing_on_it(self, tmp_path):
        directory = checkout_on(tmp_path, "work/topic")
        for command in (
            "git log main..HEAD",
            "git diff main",
            "git show origin/main:README.md",
            "git fetch origin main",
            "git merge-base main HEAD",
        ):
            assert decide(command, directory) is None, command


# ----------------------------------------------- 2. deleting outside the drawers


class TestDeletes:
    def test_a_recursive_delete_of_the_workbench_is_refused(self, project):
        assert denied(decide("rm -rf workbench", project))
        assert denied(decide("rm -rf workbench/active", project))

    def test_the_scratch_drawer_may_be_emptied(self, project):
        # Its own rule is "disposable, delete without checking". Refusing this is the
        # single change that most reduces the noise, and it is also correct.
        assert decide("rm -rf workbench/scratch", project) is None
        assert decide("rm -rf workbench/scratch/old-run", project) is None
        assert decide("rm -rf ./workbench/scratch/", project) is None

    def test_a_traversal_out_of_scratch_is_not_scratch(self, project):
        assert denied(decide("rm -rf workbench/scratch/../active", project))

    def test_an_unexpanded_variable_is_never_treated_as_disposable(self, project):
        # `p=../../..` makes this delete the home directory. Nothing here can resolve
        # it, so the exemption has to fail toward refusing.
        assert denied(decide("rm -rf workbench/scratch/$p", project))

    def test_a_mixed_delete_is_judged_by_its_worst_operand(self, project):
        # Testing whether the command merely *mentions* scratch waved `rm -rf
        # workbench/scratch ~` through with the home directory attached.
        assert denied(decide("rm -rf workbench/scratch ~", project))

    def test_temporary_directories_and_caches_are_disposable(self, project):
        assert decide("rm -rf /tmp/build", project) is None
        assert decide("rm -rf __pycache__", project) is None
        assert decide("rm -rf .pytest_cache", project) is None

    def test_a_single_file_delete_passes(self, project):
        # Constant in ordinary work, cheap to redo. Refusing it is the noise that makes
        # a real refusal invisible.
        assert decide("rm notes.md", project) is None
        assert decide("rm -f build.log", project) is None

    def test_uppercase_r_is_recursive_too(self, project):
        assert denied(decide("rm -Rf operations", project))

    def test_a_delete_of_the_home_directory_is_refused(self, project):
        assert denied(decide("rm -rf ~", project))
        assert denied(decide("rm -rf /", project))

    def test_the_end_of_options_marker_is_honoured(self, project):
        # `rm -- -rf` deletes a file named `-rf`, and reading that as a recursive
        # delete would be a refusal against a plain single-file removal.
        assert decide("rm -- -rf", project) is None

    def test_git_clean_is_refused_because_workbench_is_untracked(self, project):
        assert denied(decide("git clean -fd", project))
        assert denied(decide("git clean -fdx", project))

    def test_a_dry_run_clean_passes(self, project):
        assert decide("git clean -nd", project) is None


class TestDeletesInsideATemporaryCheckout:
    """The refusals must not evaporate because the checkout sits in a temporary
    directory.

    A checkout can land under `/tmp` — a scratch clone, a sandbox, or the throwaway
    project root these tests build. It is pytest's fixture root that put one there on
    Linux, not a chamber: a chamber clones to `/work`.
    Treating that prefix as disposable wherever it appeared meant the whole checkout
    was judged disposable, and `rm -rf` on the repository itself was waved straight
    through — the one thing this refusal exists to stop. macOS hid it by accident,
    which is why the suite passed on the host and failed in a chamber.

    The rule these pin: a temporary directory is disposable **only when it is outside
    the project root**. Inside it, only the named drawers are.
    """

    def test_the_project_itself_is_not_disposable_for_sitting_in_tmp(self, project_under_tmp):
        assert denied(decide(f"rm -rf {project_under_tmp}", project_under_tmp))

    def test_the_drawers_inside_a_tmp_checkout_are_still_refused(self, project_under_tmp):
        assert denied(decide("rm -rf workbench", project_under_tmp))
        assert denied(decide("rm -rf workbench/active", project_under_tmp))
        assert denied(decide("rm -rf operations", project_under_tmp))

    def test_scratch_inside_a_tmp_checkout_is_still_disposable(self, project_under_tmp):
        assert decide("rm -rf workbench/scratch", project_under_tmp) is None
        assert decide("rm -rf workbench/scratch/old-run", project_under_tmp) is None

    def test_a_temporary_directory_outside_the_project_is_still_disposable(self, project_under_tmp):
        assert decide("rm -rf /tmp/some-other-build", project_under_tmp) is None

    def test_the_parent_of_a_tmp_checkout_is_not_disposable(self, project_under_tmp):
        # `/tmp` itself contains the checkout, so deleting it destroys the repository
        # by a shorter path than naming it.
        assert denied(decide("rm -rf /tmp", project_under_tmp))
        assert denied(decide(f"rm -rf {project_under_tmp}/..", project_under_tmp))

    def test_an_intermediate_parent_is_not_disposable(self, project_under_tmp):
        # The case the first fix missed and all three review seats found. This path
        # is under `/tmp/`, is not inside the project, and contains it.
        assert denied(decide(f"rm -rf {project_under_tmp.parent}", project_under_tmp))

    def test_a_temporary_sibling_of_the_checkout_is_still_disposable(self, project_under_tmp):
        # The containment rule must not swallow the exemption it guards.
        assert decide(f"rm -rf {project_under_tmp.parent}/build", project_under_tmp) is None

    def test_a_symlink_from_a_temporary_directory_into_the_project_is_refused(
        self, project_under_tmp
    ):
        # `rm -rf /tmp/link/` follows the link, so the literal spelling describes
        # nothing about what would actually be deleted. Judging it alone exempted
        # this; requiring every spelling to be disposable refuses it.
        target = project_under_tmp / "operations"
        target.mkdir()
        link = Path(tempfile.mkdtemp(dir="/tmp")) / "escape"
        link.symlink_to(target, target_is_directory=True)
        assert denied(decide(f"rm -rf {link}", project_under_tmp))


# ----------------------------------------------------- 3. rewriting history


class TestHistory:
    def test_a_force_push_is_refused_in_every_spelling(self):
        assert denied(decide("git push --force origin work/topic"))
        assert denied(decide("git push -f origin work/topic"))
        assert denied(decide("git push --force-with-lease origin work/topic"))

    def test_an_ordinary_push_to_a_work_branch_passes(self):
        assert decide("git push origin work/topic") is None
        assert decide("git push -u origin infra/autoclave-container") is None

    def test_history_filters_are_refused(self):
        assert denied(decide("git filter-branch --tree-filter 'rm -f x' HEAD"))
        assert denied(decide("git filter-repo --path secrets --invert-paths"))

    def test_a_hard_reset_is_refused(self):
        assert denied(decide("git reset --hard HEAD~1"))

    def test_a_soft_or_mixed_reset_passes(self):
        assert decide("git reset --soft HEAD~1") is None
        assert decide("git reset HEAD file.py") is None

    def test_amend_and_local_rebase_pass(self):
        # Both are routine here — the reviewer-pass skill amends `Reviewed-by:`
        # trailers in after a review returns — and both are recoverable from the reflog.
        assert decide("git commit --amend --no-edit") is None
        assert decide("git rebase origin/main") is None


# --------------------------------------------------- 4. deleting a remote ref


class TestRemotes:
    def test_deleting_a_remote_branch_is_refused(self):
        assert denied(decide("git push origin --delete work/topic"))
        assert denied(decide("git push -d origin work/topic"))
        assert denied(decide("git push origin :work/topic"))
        assert denied(decide("git push --mirror origin"))

    def test_deleting_a_local_branch_passes(self):
        # Recoverable from the reflog, and routine after a merge.
        assert decide("git branch -d work/topic") is None
        assert decide("git branch -D work/topic") is None

    def test_deleting_the_repository_is_refused(self):
        assert denied(decide("gh repo delete tyrel/verbatus_alpha"))

    def test_ordinary_gh_calls_pass(self):
        assert decide("gh pr view 14") is None
        assert decide("gh pr create --title x --body y") is None
        assert decide("gh api repos/tyrel/verbatus_alpha/pulls") is None


# ------------------------------------------------- 5. a credential into git

# Assembled at runtime rather than written out. The `pre-commit` ingress scan reads
# this file as it reads any other and refuses a recognized credential in it — which it
# did, on the first attempt to commit this suite. A test fixture that trips the real
# scanner is the scanner working; splitting the literal is how a fixture stays a
# fixture. It is not a real key in any spelling.
FAKE_KEY = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuv"


class TestCredentials:
    def test_staging_the_private_drawer_is_refused(self):
        assert denied(decide("git add private/ntfy.conf"))
        assert denied(decide("git add -f private/"))

    def test_staging_ordinary_paths_passes(self):
        assert decide("git add operations/autoclave/autoclave.sh") is None

    def test_writing_a_key_into_tracked_ground_is_refused(self, project):
        decision = decide_write(
            project / "operations" / "notify" / "notify.sh",
            f"TOKEN={FAKE_KEY}\n",
            cwd=project,
        )
        assert denied(decision)

    def test_the_same_key_may_be_written_into_the_gitignored_drawers(self, project):
        # `private/` is where a secret is *supposed* to live. Refusing it there would
        # make the correct action the harder one.
        for drawer in ("private", "workbench"):
            assert (
                decide_write(
                    project / drawer / "conf",
                    f"TOKEN={FAKE_KEY}\n",
                    cwd=project,
                )
                is None
            )

    def test_ordinary_prose_is_not_a_credential(self, project):
        assert decide_write(project / "README.md", "The token lives in private/.", project) is None


# --------------------------------------------- 6. switching the git hooks off


class TestHookBypass:
    def test_no_verify_is_refused(self):
        assert denied(decide("git commit --no-verify -m 'x'"))
        assert denied(decide("git push --no-verify origin work/topic"))
        assert denied(decide("git commit -n -m 'x'"))
        assert denied(decide("git commit -an -m 'x'"))

    def test_an_ordinary_commit_message_containing_n_is_not_a_bypass(self):
        # `-mno` carries the message "no". Reading its `n` as `--no-verify` would
        # refuse a plain commit, which is exactly the class of false alarm that
        # retired the last guard.
        assert decide("git commit -mno") is None
        assert decide("git commit -am 'note on the branch'") is None

    def test_pointing_hookspath_elsewhere_is_refused(self):
        assert denied(decide("git -c core.hooksPath=/dev/null commit -m x"))
        assert denied(decide("git -ccore.hooksPath= push origin work/topic"))
        assert denied(decide("git config core.hooksPath /dev/null"))
        assert denied(decide("git config --unset core.hooksPath"))

    def test_reading_hookspath_passes(self):
        # `install.sh` and every session check reads it. Refusing a read would make
        # confirming the guard is armed impossible.
        assert decide("git config --get core.hooksPath") is None
        assert decide("git config core.hooksPath") is None


# ------------------------------- 7. an agent reaching for a governed path


def decide_as_agent(tool: str, tool_input: dict, agent: str = "general-purpose"):
    return guard.evaluate(
        {
            "tool_name": tool,
            "tool_input": tool_input,
            "cwd": str(ROOT),
            "agent_type": agent,
            "agent_id": "agent-123",
        }
    )


class TestAgentGovernance:
    """The one asymmetry in the file, and the reason built-ins can be used at all.

    The session may edit these — it has read CLAUDE.md and prompting it on every
    governed edit is what retired the last guard. `general-purpose` and `Explore` are
    Claude Code's own roles: they carry no project instruction and they hold `Bash`.
    """

    def test_an_agent_may_not_write_a_governing_document(self):
        assert denied(decide_as_agent("Write", {"file_path": str(ROOT / "CLAUDE.md")}))
        assert denied(decide_as_agent("Edit", {"file_path": str(ROOT / "GOVERNANCE.md")}))

    def test_an_agent_may_not_write_under_dot_claude(self):
        for path in ("settings.json", "hooks/guard.py", "agents/scout.md"):
            assert denied(decide_as_agent("Write", {"file_path": str(ROOT / ".claude" / path)})), (
                path
            )

    def test_an_agent_may_not_reach_them_through_a_shell_either(self):
        # Explore and Plan hold Bash. Withholding Edit does not make a role read-only.
        assert denied(decide_as_agent("Bash", {"command": "echo x > CLAUDE.md"}, "Explore"))
        assert denied(decide_as_agent("Bash", {"command": "sed -i '' s/a/b/ GOVERNANCE.md"}))

    def test_an_agent_may_write_anything_else(self):
        # The point is a bound, not a cage. Ordinary work is untouched.
        assert decide_as_agent("Write", {"file_path": str(ROOT / "operations" / "x.py")}) is None
        assert decide_as_agent("Bash", {"command": "pytest -q"}) is None
        assert decide_as_agent("Bash", {"command": "grep -rn TODO CLAUDE.md"}) is None

    @pytest.mark.parametrize(
        "command",
        (
            "printf x > .claude/settings.json",
            "printf x > /abs/path/.claude/hooks/guard.py",
            "tee .claude/settings.json",
            "cp /dev/null .claude/settings.json",
            "mv evil.py .claude/hooks/guard.py",
        ),
    )
    def test_an_agent_cannot_reach_dot_claude_through_a_shell_path(self, command):
        # The redirect branch matched governed *basenames* only, and `guard.py` is not
        # one — so an agent could overwrite this file itself. The `.claude/` alternative
        # required a leading slash, so the bare relative spelling passed too. Found by a
        # Terra seat running the guard inside a chamber; DEFERRED_ACTIONS row 0.
        assert denied(decide_as_agent("Bash", {"command": command})), command

    @pytest.mark.parametrize(
        "command",
        ("grep -r foo .claude/", "cat .claude/settings.json", "ls .claude/"),
    )
    def test_an_agent_may_still_read_dot_claude(self, command):
        # Reading is the whole job of a reader seat. A refusal here would be noise.
        assert decide_as_agent("Bash", {"command": command}) is None, command

    def test_the_main_session_is_not_touched_by_this(self):
        # Named explicitly: the asymmetry is the design, not a leak.
        assert decide_write(ROOT / "CLAUDE.md") is None
        assert decide("echo x > CLAUDE.md") is None

    def test_the_refusal_tells_the_agent_what_to_do_instead(self):
        decision = decide_as_agent("Write", {"file_path": str(ROOT / "CLAUDE.md")})
        assert "propose exact wording" in decision[1].lower()


# --------------------------------------------------- seeing through `rtk proxy`


class TestRtkProxyIsTransparent:
    """Every refusal must survive the wrapper this project tells sessions to use.

    `rtk` was listed as a wrapper, which looked like coverage. `rtk proxy` is two
    tokens: `proxy` landed where a command name was expected, nothing matched, and
    the guard returned no decision at all for whatever followed. `RTK.md` tells a
    session to reach for `rtk proxy <cmd>` whenever a summary cannot be trusted, so
    the blind spot sat exactly on the documented habit rather than on an odd spelling
    nobody uses. A previous branch fixed this against the 2,294-line guard; the
    rewrite did not carry it across, and nobody had checked which.
    """

    def test_a_push_at_main_is_refused_behind_the_proxy(self):
        assert denied(decide("rtk proxy git push origin main"))
        assert denied(decide("rtk git push origin main"))

    def test_a_recursive_delete_is_refused_behind_the_proxy(self, project):
        assert denied(decide("rtk proxy rm -rf workbench", project))

    def test_a_force_push_is_refused_behind_the_proxy(self):
        assert denied(decide("rtk proxy git push --force origin work/topic"))

    def test_a_hook_bypass_is_refused_behind_the_proxy(self):
        assert denied(decide("rtk proxy git commit --no-verify -m x"))
        assert denied(decide("rtk proxy git config core.hooksPath /dev/null"))

    def test_the_proxy_over_ordinary_work_stays_silent(self, project):
        # The point of the wrapper is that it is used constantly. Reading through it
        # must not turn every proxied command into a refusal.
        for command in ("rtk proxy git status", "rtk proxy pytest -q", "rtk proxy ls"):
            assert decide(command, project) is None, command

    def test_a_command_inside_a_subshell_or_brace_group_is_seen(self, project):
        # `(` and `{` open a command position exactly as `;` does. Without them every
        # refusal returned silence behind one bracket, and subshells are not among
        # the limits this guard's docstring declares.
        assert denied(decide("(git push origin main)", project))
        assert denied(decide("{ rm -rf workbench; }", project))

    def test_a_plus_refspec_is_a_force_push(self):
        # `+` forces without any flag at all.
        assert denied(decide("git push origin +main"))
        assert denied(decide("git push origin +refs/heads/x:refs/heads/y"))

    def test_hookspath_cannot_be_set_through_a_config_file_option(self):
        # `--file <path>` put the path where the setting name was expected and walked
        # straight past the check that exists to close this exact family.
        assert denied(decide("git config --file .git/config core.hooksPath /dev/null"))
        assert denied(decide("git config -f .git/config core.hooksPath /dev/null"))

    def test_reading_config_through_a_file_option_still_passes(self):
        assert decide("git config --file .git/config core.hooksPath") is None

    def test_a_symlinked_cache_directory_is_not_disposable(self, project_under_tmp):
        # Judged on the name alone, `node_modules` pointing somewhere real elsewhere
        # was waved through — the one branch still reading the literal text by itself.
        target = project_under_tmp / "operations"
        target.mkdir()
        link = Path(tempfile.mkdtemp(dir="/tmp")) / "node_modules"
        link.symlink_to(target, target_is_directory=True)
        assert denied(decide(f"rm -rf {link}", project_under_tmp))

    @pytest.mark.parametrize(
        "prefix",
        (
            "rtk proxy",
            "rtk proxy --",
            "rtk proxy --skip-env",
            "rtk proxy --ultra-compact",
            "rtk --skip-env proxy",
            "rtk -v proxy",
            "env",
        ),
    )
    def test_no_option_or_wrapper_spelling_hides_a_refusal(self, prefix):
        # Every one of these passed unseen after the first fix. `rtk` takes global
        # flags before its subcommand and `proxy` takes its own; naming today's four
        # would work until rtk gains a fifth, so flags are matched generically. `env`
        # is as ordinary as `nohup` and was simply missing from the list.
        assert denied(decide(f"{prefix} git push origin main")), prefix

    def test_rtks_own_subcommands_are_not_wrappers(self, project):
        # `gain` and `discover` run nothing after them; treating them as wrappers
        # would be reading a report as the thing it reports on.
        for command in ("rtk gain", "rtk gain --history", "rtk discover"):
            assert decide(command, project) is None, command


# ------------------------------------------------------------------- Silence


class TestSilence:
    """Commands that must pass without a word.

    Most of these were asked about by the predecessor. Together they are most of what
    a session does in a day, and they are the reason approval became reflexive.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q",
            "git status",
            "git diff --stat",
            "git switch -c work/topic",
            "git worktree add ../wt work/topic",
            "docker build -t verbatus-autoclave .",
            "sh operations/autoclave/autoclave.sh doctor",
            "sh operations/notify/notify.sh milestone 'the chamber works end to end'",
            "curl -fsSL https://example.com/spec.json",
            "gh pr comment 14 --body 'fixed in ce6d8bd'",
            "ruff check .",
            "mv workbench/active/HANDOFF.md workbench/archive/",
            "python3 -m venv .venv",
            "runpodctl stop pod abc",
        ],
    )
    def test_ordinary_work_passes(self, command):
        assert decide(command) is None, command

    def test_editing_a_governed_document_passes(self):
        # It no longer asks. CLAUDE.md hard rule 10 governs this, and the session has
        # read it; a prompt on every edit to a document the session edits legitimately
        # is what trained the reflex. The git hooks still police what lands.
        assert decide_write(ROOT / "CLAUDE.md") is None
        assert decide_write(ROOT / ".claude" / "settings.json") is None

    def test_a_heredoc_body_is_data_rather_than_a_command(self, tmp_path):
        # Writing a document that quotes a dangerous command must not read as one.
        # With no ask verdict left, this mistake now costs a blocked command.
        command = "cat > notes.md <<'EOF'\ngit push --force origin main\nrm -rf workbench\nEOF"
        assert decide(command, checkout_on(tmp_path, "work/topic")) is None

    @pytest.mark.parametrize(
        "command",
        [
            # Found the hard way: the guard refused its own author's test fixture,
            # within an hour of the file being written. Quoted text is data.
            "printf 'build it; rm -rf /; then check'",
            'git commit -m "x; git push --force origin main"',
            "echo 'to clean up: rm -rf workbench'",
            'rg "rm -rf" operations/',
            "git commit -m 'document the rm -rf workbench hazard'",
            'gh pr comment 14 --body "do not run gh repo delete"',
        ],
    )
    def test_a_command_named_inside_a_string_is_not_a_command(self, command):
        assert decide(command) is None, command

    def test_a_quoted_operand_still_refuses_rather_than_disappearing(self, project):
        # The other direction, and the one that must not be got wrong: blanking a
        # quoted target must not turn a real deletion into an argument-less one that
        # reads as harmless. An empty target list is not "all disposable".
        assert denied(decide('rm -rf "$HOME"', project))
        assert denied(decide("rm -rf 'workbench'", project))

    def test_a_later_line_is_still_read(self, tmp_path):
        # The counterpart: a harmless first line must not hide what follows it.
        command = "echo starting\ngit push --force origin work/topic"
        assert denied(decide(command, checkout_on(tmp_path, "work/topic")))


# ------------------------------------------------------- shape and wiring


class TestShape:
    def test_the_guard_never_asks(self):
        # The whole premise. An `ask` verdict reintroduces the failure this file was
        # rewritten to remove, so it is pinned rather than left to discipline.
        code = "\n".join(
            line
            for line in SCRIPT.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert '"ask"' not in code and "'ask'" not in code

    def test_evaluate_runs_every_refusal(self):
        # Seven, not the five Tyrel named. Six is the hook-bypass check, flagged as
        # one past the spec in `switching_the_hooks_off`. Seven is the agent-only
        # governed-path check, added when built-in agent types were allowed back in.
        # An eighth arriving without that conversation fails here.
        assert len(guard.CHECKS) == 7

    def test_an_unreadable_payload_fails_closed(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True
        )
        assert result.returncode == 2

    def test_a_permitted_call_says_nothing_at_all(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input=payload, capture_output=True, text=True
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_a_refusal_emits_the_hook_protocol(self):
        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git push --force origin x"}}
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input=payload, capture_output=True, text=True
        )
        emitted = json.loads(result.stdout)["hookSpecificOutput"]
        assert emitted["hookEventName"] == "PreToolUse"
        assert emitted["permissionDecision"] == "deny"
        assert emitted["permissionDecisionReason"]

    def test_the_guard_is_wired_or_the_readme_says_it_is_not(self):
        # Hard rule 11: switching it off is allowed, and doing so silently is not.
        # This is the check that makes the README's Controls section load-bearing.
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
        if "guard.py" in json.dumps(hooks.get("PreToolUse", [])):
            return
        declared = "The tool-call guard is currently switched OFF" in (
            ROOT / "README.md"
        ).read_text(encoding="utf-8")
        assert declared, (
            "no PreToolUse hook invokes guard.py, and README.md does not declare it off. "
            "Wire it back in, or declare the suspension where a reader will find it."
        )

    def test_the_decision_log_never_records_the_command(self, tmp_path, monkeypatch):
        # A refused command is exactly the kind that may carry a credential.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        (tmp_path / "private").mkdir()
        secret = "git push --force https://user:hunter2@example.com/repo"
        guard.record({"tool_name": "Bash", "tool_input": {"command": secret}}, "deny", "a reason")
        written = (tmp_path / guard.DECISION_LOG).read_text(encoding="utf-8")
        assert "hunter2" not in written
        assert "deny" in written
