"""What the guard must and must not block.

Every case here came from an adversarial audit finding, so this file is a
record of things that were once wrong. Deleting a case throws away the only
evidence that it was ever fixed.

Nothing here is executed as a shell command. Each string is handed to the
guard as data, exactly as Claude Code hands it over: JSON on stdin.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard.py")
PROJECT = "/Users/tyrel/verbatus_alpha"
HOME = "/Users/tyrel"

# Split so that this file does not itself read as a command anyone runs.
RC = "runpod" + "ctl"
REST = "rest." + "runpod" + ".io"
API = "api." + "runpod" + ".io"


def decide(payload) -> bool:
    """True if the guard denies. Runs guard.py exactly as the harness does."""
    result = subprocess.run(
        [sys.executable, GUARD],
        input=json.dumps(payload) if isinstance(payload, dict) else payload,
        capture_output=True,
        text=True,
        # Fixed cwd: relative operands like `.venv` resolve against it, so a
        # result must not depend on where the suite happens to be run from.
        cwd=tempfile.gettempdir(),
        env={"CLAUDE_PROJECT_DIR": PROJECT, "HOME": HOME, "PATH": "/usr/bin:/bin"},
    )
    return '"deny"' in result.stdout


def run(command: str) -> bool:
    return decide({"tool_name": "Bash", "tool_input": {"command": command}})


# --------------------------------------------------------------- must block

BLOCK = [
    # RunPod, by every route an audit found open
    (f"{RC} create pod --gpuType A100", "creates a pod"),
    (f"{RC} start pod abc123", "wakes a stopped pod, which bills again"),
    (f"{RC} project deploy", "billed infrastructure"),
    (f"{RC} project dev", "billed session"),
    (f"{RC} remove pods myname", "bulk destroy"),
    (f"{RC} create pod --name help", "'help' as a value must not disarm the check"),
    (f"env FOO=1 {RC} create pod", "hidden behind env"),
    (f"timeout 300 {RC} create pod", "wrapper with a numeric argument"),
    (f"timeout -s KILL 30 {RC} create pod", "wrapper option that takes a value"),
    (f"xargs -I {{}} {RC} create pod", "xargs -I takes a value"),
    (f"sudo env {RC} create pod", "two wrappers deep"),
    (f"for p in a b; do {RC} remove pods $p; done", "inside a loop"),
    (f"curl -X POST https://{REST}/v1/pods -d @body.json", "REST create"),
    (f"curl -d @body.json https://{REST}/v1/pods", "-d is a POST"),
    (f"curl --json @b.json https://{REST}/v1/pods", "--json is a POST"),
    (f"curl -XPOST https://{REST}/v1/pods", "attached -XPOST"),
    (f"wget --method=POST --body-data='{{}}' https://{REST}/v1/pods", "wget's syntax"),
    (f"http POST https://{REST}/v1/pods name=big", "httpie positional verb"),
    (f"curl -X POST https://{REST.upper()}/v1/pods -d @b.json", "upper-case host"),
    (f"curl -X DELETE https://{REST}/v1/networkvolumes/vol-1", "volume delete"),
    # -g is --globoff, not --get. Lowercasing the test disarmed the whole
    # write check, and four paired audit rounds missed it; CodeRabbit found it.
    (f"curl -g -X POST https://{REST}/v1/pods -d @b.json", "-g is --globoff"),
    (f"curl --globoff -X POST https://{REST}/v1/pods -d @b.json", "the long form"),
    (f"curl -g -X DELETE https://{REST}/v1/networkvolumes/v1", "volume delete behind -g"),
    (
        f'curl -X POST https://{API}/graphql -d \'{{"query":"mutation{{podResume}}"}}\'',
        "graphql resume",
    ),
    ('python3 -c "import runpod; runpod.create_pod()"', "the SDK"),
    ('python3 -c "from runpod import create_pod; create_pod()"', "the from-import form"),
    ('python3.11 -c "import runpod; runpod.create_pod()"', "a versioned interpreter"),
    ('uv run python -c "import runpod; runpod.create_pod()"', "a runner front-end"),
    ('poetry run python -c "import runpod; runpod.create_pod()"', "another runner"),
    ('python3 -c "import runpod; runpod.create_endpoint()"', "serverless endpoint"),
    # Turning the hooks off
    ("git -c core.hooksPath=/dev/null push origin work/x", "one-shot"),
    ("git config core.hooksPath /dev/null", "permanent, for every tool"),
    ("git config --global core.hooksPath /tmp/empty", "permanent and global"),
    # main, force, and skipping the hooks
    ("git push origin main", "main moves only by merge"),
    ("git push origin HEAD:main", "refspec form"),
    ("git push origin HEAD:refs/heads/main", "long refspec form"),
    ("git push origin +work/x:work/x", "+ is a force-push"),
    ("git push --force origin work/x", "force"),
    ("git push --force-with-lease origin work/x", "force"),
    ("git push -fu origin work/x", "combined short flags"),
    ("git push -uf origin work/x", "combined short flags, other order"),
    ("git push --no-verify origin work/x", "skips the hooks"),
    ("git commit --no-verify -m x", "skips the hooks"),
    ("git commit -n -m x", "the short form"),
    ("git merge --no-verify other", "merge skips hooks too"),
    (
        "ALLOW_FORCE_PUSH=1 git push --force origin work/x",
        "a bare VAR= prefix must not blind the dispatcher",
    ),
    ("ALLOW_MAIN_PUSH=1 git push origin main", "same, with main"),
    ("sudo -u tyrel git push origin main", "a wrapper's value must not become the command"),
    ("git push \\\n  origin main", "backslash line continuation"),
    ("set -e\ncd /x\ngit push origin main", "a newline is a separator"),
    ("echo a#b && git push origin main", "# mid-word must not swallow the rest"),
    ("git log --format=%h#%s && git push --force origin main", "# in a format string"),
    # deleting what must not be deleted
    (f"rm -rf {PROJECT}", "the repository"),
    (f"rm -rf {PROJECT}/*", "the repository's contents"),
    ("rm -rf ~", "your home"),
    ("rm -rf ~/*", "your home's contents"),
    ("rm -rf $HOME", "an unexpanded variable"),
    ('rm -rf "$HOME"', "a quoted variable"),
    ("rm -rf /", "the obvious one"),
    ("rm -rf /*", "the whole disk"),
    ("rm -rf .git", "history, and the audit receipts"),
    ("rm -rf .githooks", "the enforcement itself"),
    ("rm -rf .claude", "the guard itself"),
    (f"rm -r {PROJECT}", "-r without -f still deletes"),
    # a wrapper's boolean flag must not swallow the real command
    (f"sudo -E {RC} create pod", "-E takes no value in sudo"),
    (f"env -i {RC} create pod", "-i takes no value in env"),
    ("sudo -n rm -rf ~", "-n takes no value in sudo"),
    ("sudo -E git push origin main", "same, with main"),
    # a command carried as a string
    (f"bash -c '{RC} create pod'", "bash -c payload"),
    ("sh -c 'rm -rf ~'", "sh -c payload"),
    ('eval "git push origin main"', "eval payload"),
    (f"timeout 600 bash -c '{RC} create pod'", "wrapper plus bash -c"),
    (f"setsid nohup bash -c '{RC} create pod' > /tmp/j.log 2>&1", "detached launch"),
    # ...and must not swallow the flag after it either: `-E` ate the `-u`,
    # which left `tyrel` in the command position and skipped every check
    (f"sudo -E -u tyrel {RC} create pod", "a boolean flag eating the next flag"),
    ("sudo -n -u tyrel rm -rf ~", "same, with rm"),
    (f"env -i -u PATH {RC} create pod", "same, with env"),
    (f"sudo -E -u tyrel bash -c '{RC} create pod'", "same, wrapping a payload"),
    (f"rm -rf {PROJECT}/.claude/worktrees", "every other agent's uncommitted work"),
    ("rm -rf ..", "the directory above the working one contains it"),
    (
        'echo "see <<EOF for the syntax"\nrm -rf ~',
        "an unterminated heredoc marker must not hide what follows",
    ),
    # rtk is this machine's always-on proxy
    ("rtk git push --force origin work/x", "behind the rtk proxy"),
    (f"rtk proxy {RC} create pod", "behind rtk proxy"),
    ("aws s3 rm s3://bucket/prefix --recursive", "irreversible"),
    ("aws --profile prod s3 rm s3://b/p --recursive", "behind a profile"),
    ("/usr/local/bin/aws s3 rm s3://b/p --recursive", "by full path"),
]

# ----------------------------------------------------------- must NOT block

ALLOW = [
    # money-saving and state-verifying: GOVERNANCE 8 requires these
    (f"{RC} stop pod abc", "stopping saves money"),
    (f"{RC} remove pod abc", "removing one named pod is cleanup"),
    (f"{RC} get pod", "read-only"),
    (f"{RC} create --help", "reading the manual is free"),
    (f"curl -s https://{REST}/v1/pods", "a plain GET"),
    (f"curl -f -sS https://{REST}/v1/pods/abc", "curl -f is --fail, a read"),
    (
        f"curl -G https://{API}/graphql --data-urlencode 'query={{myself{{id}}}}'",
        "-G makes it a GET",
    ),
    (f"curl --get https://{API}/graphql --data-urlencode 'q=1'", "the long form of -G"),
    (f"curl -x proxy.local:8080 https://{REST}/v1/pods", "lower -x is --proxy, a read"),
    (f"curl -X POST https://{REST}/v1/pods/abc/stop", "shutdown must be verifiable"),
    (f"curl -s https://{REST}/v1/networkvolumes", "listing volumes is a read"),
    ("python3 -c 'import runpod; print(runpod.get_pods())'", "reading state"),
    # reading and writing *about* the guarded things
    ("grep -rn networkvolume .", "reading"),
    ("rg networkvolume docs/", "reading"),
    ("git log --grep=networkvolume", "reading"),
    ("git commit -m 'note the networkvolume rule'", "writing about it"),
    ("git commit -m 'document the --no-verify escape hatch'", "a flag in a message"),
    ("git commit -m 'guard blocks git push --force now'", "a flag in a message"),
    (
        'git commit -m "Close the rm gap\n\nrm -rf handling was wrong."',
        "a multi-line message about rm",
    ),
    (
        "cat > notes.md <<'EOF'\nrm -rf ~ would destroy the home directory.\nEOF",
        "a heredoc body is not executed",
    ),
    # ordinary work
    ("git push -u origin work/main-fix", "a branch whose name contains main"),
    ("git push -u origin infra/main-guard", "likewise"),
    ("git push -u origin work/domain-model", "main inside a longer word"),
    ("git push -u origin infra/guard-gaps && gh pr create --base main", "opening a pull request"),
    ("git push origin work/x && git switch main", "chained and harmless"),
    ("git push origin work/x && docker build -f Dockerfile .", "docker -f"),
    ("git push origin work/x && grep -f patterns.txt notes.txt", "grep -f"),
    ("git push --dry-run origin work/x", "a dry run changes nothing"),
    ("git push -u origin work/x", "-u alone is not force"),
    ("git log --oneline main", "reading main"),
    ("git status\ngit diff --stat", "ordinary multi-line"),
    ("git commit -m 'wip' && grep -n TODO guard.py", "grep -n after a commit"),
    ("git push origin work/x  # never use -f here", "a comment is not a flag"),
    # cleanup CLAUDE.md explicitly permits
    ("rm -rf workbench/scratch/*", "anyone may delete anything here without asking"),
    ("rm -rf .venv __pycache__", "ordinary cleanup"),
    ("rm -rf node_modules && npm install", "ordinary cleanup"),
    ("rm -rf build/*", "ordinary cleanup"),
    ('rm -rf "$SCRATCH/session"', "an unknown variable is not a threat"),
    ("aws s3 ls s3://bucket", "read-only"),
    # reading the hooks setting is how you check install.sh worked
    ("git config --get core.hooksPath", "the read form"),
    ("git config --list", "reading all config"),
    ("sh .githooks/install.sh", "the sanctioned way to set it"),
    # a <<WORD in prose is not a heredoc, and must not hide what follows
    ('echo "see <<EOF for the syntax"\ngit status', "no closing tag, nothing hidden"),
    # a body field named deleteAfter is not a shutdown
    (f"curl -X DELETE https://{REST}/v1/pods/abc", "removing your own pod is cleanup"),
]


@pytest.mark.parametrize("command,why", BLOCK, ids=[c for c, _ in BLOCK])
def test_blocks(command, why):
    assert run(command), f"should have been blocked ({why})"


@pytest.mark.parametrize("command,why", ALLOW, ids=[c for c, _ in ALLOW])
def test_allows(command, why):
    assert not run(command), f"should have been allowed ({why})"


MCP_BLOCK = [
    "mcp__runpod__create-pod",
    "mcp__runpod__start-pod",
    "mcp__runpod__resume_pod",
    "mcp__runpod__create_endpoint",
    "mcp__runpod__delete_network_volume",
    "mcp__6c85a858__runpod_create_pod",
]
MCP_ALLOW = [
    "mcp__runpod__get-pods",
    "mcp__runpod__stop_pod",
    "mcp__runpod__terminate_pod",
    "mcp__runpod__delete_pod",
    "mcp__github__create-issue",
]


@pytest.mark.parametrize("tool", MCP_BLOCK)
def test_blocks_mcp(tool):
    assert decide({"tool_name": tool, "tool_input": {}}), f"{tool} should be blocked"


@pytest.mark.parametrize("tool", MCP_ALLOW)
def test_allows_mcp(tool):
    # stop / terminate / delete end billing, and GOVERNANCE 8 requires shutdown
    # to be verified against provider state. Blocking them fights the rule.
    assert not decide({"tool_name": tool, "tool_input": {}}), f"{tool} should be allowed"


MALFORMED = [
    '{"tool_name":"Bash","tool_input":null}',
    '{"tool_name":"Bash","tool_input":{"command":["rm","-rf","/"]}}',
    '{"tool_name":"Bash","tool_input":[]}',
    '{"tool_name":[],"tool_input":{}}',
    '{"tool_name":"Bash","tool_input":{}}',
    "not json at all",
    "[]",
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_fails_closed(payload):
    """A check that cannot run is a failure, not a pass — GOVERNANCE.md 10."""
    assert decide(payload), "malformed input must deny, never fall through"


BLOCK_EXTRA = [
    (
        f"curl -X POST https://{REST}/v1/pods/abc -d '{{\"deleteAfter\":1}}'",
        "a body field named deleteAfter is not a shutdown",
    ),
]


@pytest.mark.parametrize("command,why", BLOCK_EXTRA, ids=[c for c, _ in BLOCK_EXTRA])
def test_blocks_extra(command, why):
    assert run(command), f"should have been blocked ({why})"
