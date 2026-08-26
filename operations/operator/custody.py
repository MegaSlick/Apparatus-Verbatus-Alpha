"""Launch the console and its decision worker under an OS-enforced boundary.

Two layers hold the custody promise, and they are deliberately independent:
the renderer's own source cannot reach a writer (checked statically), and the
operating system refuses the write even if it could. This module owns the
second layer, and it owns it *per platform*.

**There is one contract and one seam.** A confinement denies every filesystem
mutation except, at most, one explicit path — the run tree's append-only
decision-record directory. Each platform supplies its own backend behind that
seam:

  Linux   Landlock, through util-linux ``setpriv``. Inherited across ``exec``
          and unrelaxable by the child.
  macOS   Seatbelt, through ``sandbox-exec`` and an SBPL profile. Inherited by
          descendants for the same reason.
  other   No backend at all — and that is stated, never assumed. A platform
          with no confinement refuses to launch and says exactly what is and
          is not enforced on it, because a console that silently ran
          unconfined would be claiming a boundary it does not have.

**Enforcement is verified, not inferred.** Before either child starts, the
chosen backend is made to prove itself: it runs a probe that attempts one
write outside the permitted path and one outbound connect, and must report
that both were refused (socket creation alone is unmediated under Seatbelt,
so the probe measures the operation the boundary actually forbids).
A backend that cannot be launched, or that launches and does not deny, is a
custody refusal. This is what makes the seam safe to extend to a platform the
author cannot execute: a profile that fails to apply cannot pass the probe, so
the worst outcome of a wrong profile is a loud refusal rather than an
unconfined console.
"""

from __future__ import annotations

import errno
import json
import os
import platform as host_platform
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Iterator, Sequence

from operations.pod.models import _looks_like_credential_field

from .errors import ErrorCode, OperatorError

_WRITE_RIGHTS: Final = (
    "write-file,remove-dir,remove-file,make-char,make-dir,make-reg,make-sock,"
    "make-fifo,make-block,refer,truncate"
)
# Named prefixes are the providers this codebase calls today; they exist so the
# common case reads as an intentional list, not only a heuristic. They are not
# the only test applied below: the Perlector chair is contractually swappable
# to "an unaltered vendor model" (ARCHITECTURE.md), so a future chair's
# credential need not start with one of these four words, and a scrubber that
# only knew today's providers would silently stop working the day a new one is
# wired in. `_looks_like_credential_field` is the same name-shape marker scan
# `operations/pod/models.py` already uses to keep a credential out of a
# durable controller receipt; reusing it here means both boundaries share one
# definition of "looks like a secret" rather than drifting apart.
PROVIDER_ENV_PREFIXES: Final = ("RUNPOD_", "AWS_", "HF_", "HUGGINGFACE_")

# Variables that decide *what code the interpreter loads*, which is a different
# concern from a credential and is dropped for a different reason. The advance
# worker is the one process in this design that may write into the run tree, so
# whoever controls its imports controls what it writes there: an inherited
# `PYTHONPATH`, a user site-packages directory, or a preloaded shared object
# could shadow `advance.py` and mint a record this module never wrote.
_INTERPRETER_CONTROL_PREFIXES: Final = ("PYTHON", "LD_", "DYLD_")

# This is deliberately a closed allowlist, not "everything that does not look
# secret". A name such as CLAUDE_CONFIG_DIR is not itself a credential, but it
# points at a credential store and has no business crossing this boundary. The
# renderer and worker need only terminal/locale presentation facts. Linux used
# to pass the broad scrubbed mapping to `setpriv --reset-env`, which then
# replaced it with HOME/SHELL/USER/LOGNAME/PATH, while Seatbelt passed the broad
# mapping unchanged. Removing that launcher rewrite and constructing one exact
# mapping here makes both platforms execute with the same environment.
_CUSTODY_ENVIRONMENT_NAMES: Final = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "NO_COLOR", "TERM", "TZ"}
)

# Isolated mode implies `-E` and `-s`, removes the current directory from the
# interpreter's implicit import path, and `-S` prevents a system `.pth` or
# `sitecustomize` hook from running before this repository chooses its import
# roots explicitly. The module command below inserts the loaded checkout and
# the interpreter's already-active package directories only after isolated
# startup has completed; it never executes their `.pth` or customization hooks.
CHILD_INTERPRETER_FLAGS: Final = ("-I", "-S")


def _is_credential_env_name(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in PROVIDER_ENV_PREFIXES) or (
        _looks_like_credential_field(key)
    )


def credential_free_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Copy a process environment with all known provider credentials removed."""

    values = dict(os.environ if source is None else source)
    return {key: value for key, value in values.items() if not _is_credential_env_name(key)}


def custody_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """The exact small environment either platform's confined child receives.

    Layered on `credential_free_environment` rather than folded into it,
    because the two removals answer different questions. A credential must not
    cross the boundary because the child must not be able to spend money. An
    interpreter or dynamic-loader control must not cross it because the child
    must run the code this repository wrote and no other.
    """

    values = credential_free_environment(source)
    return {
        key: value
        for key, value in values.items()
        if key in _CUSTODY_ENVIRONMENT_NAMES
        and not any(key.startswith(prefix) for prefix in _INTERPRETER_CONTROL_PREFIXES)
    }


_RUN_MODULE_SOURCE: Final = (
    "import json, runpy, sys\n"
    "checkout = sys.argv.pop(1)\n"
    "module = sys.argv.pop(1)\n"
    "package_roots = json.loads(sys.argv.pop(1))\n"
    "sys.path.insert(0, checkout)\n"
    "sys.path.extend(package_roots)\n"
    "runpy.run_module(module, run_name='__main__')\n"
)


def python_module_command(module: str, *arguments: str) -> list[str]:
    """Name one loaded-checkout module and known roots after isolated startup.

    The import root is fixed to the checkout that loaded this custody
    boundary, and this function takes no workspace argument at all, so no
    caller can nominate the tree the confined child imports from. The child's
    *working directory* is a separate decision the trusted parent makes at
    `run_confined(cwd=...)`, and the separation is deliberate: under an
    installed wheel the operator's workspace is legitimately not this checkout
    (see the `--workspace` default in `cli.py`), so the two cannot be checked
    against each other. Isolated startup keeps that working directory off
    `sys.path`, which is what makes holding them apart safe.
    """

    root = Path(__file__).resolve().parents[2]
    # Derive these from the interpreter installation, never from `sys.path`:
    # PYTHONPATH has already influenced the parent interpreter's `sys.path` by
    # the time this function runs, and forwarding a conveniently named attacker
    # directory would launder the very import hook the child environment drops.
    package_roots = sorted(
        {
            str(path.resolve())
            for name in ("purelib", "platlib")
            if (raw := sysconfig.get_path(name)) and (path := Path(raw)).is_dir()
        }
    )
    return [
        str(Path(sys.executable).resolve()),
        *CHILD_INTERPRETER_FLAGS,
        "-c",
        _RUN_MODULE_SOURCE,
        str(root),
        module,
        json.dumps(package_roots),
        *arguments,
    ]


def require_no_provider_credentials(environment: dict[str, str] | None = None) -> None:
    """Refuse before UI launch when a provider secret would cross its boundary.

    Called on the *scrubbed* environment right before it crosses into the
    child process — a self-check on `credential_free_environment`'s own
    output, not a gate on the launcher's ordinary shell. An operator who has
    `RUNPOD_API_KEY` exported for other verbs is the common case, not a
    problem; scrubbing it for the console is silent by design. What must not
    be silent is the scrub itself failing to catch something it claims to
    catch.
    """

    leaked = sorted(
        key
        for key in (os.environ if environment is None else environment)
        if _is_credential_env_name(key)
    )
    if leaked:
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail="provider credential names were present for the UI: " + ", ".join(leaked),
        )


# ``setpriv`` exits with this code when it establishes no privilege change at
# all -- including "Landlock is not supported"/"is supported but currently
# disabled" on a kernel without it -- and therefore never execs the wrapped
# program (util-linux ``sys-utils/setpriv.c``, ``SETPRIV_EXIT_PRIVERR = 127``,
# and ``sys-utils/setpriv-landlock.c``'s ``do_landlock`` on
# ``landlock_create_ruleset`` failure; both read from the util-linux source at
# https://github.com/util-linux/util-linux on 2026-08-22). The wrapped console
# or advance worker never runs, so this is a custody-boundary refusal, not a
# fact about the run tree or the advance request it was never given.
SETPRIV_PRIVILEGE_FAILURE_EXIT: Final = 127

# ``sandbox-exec`` prefixes its own diagnostics with its program name and never
# execs the target when the profile fails to compile or apply, so a line
# beginning this way is the launcher speaking, not the console
# (`sandbox-exec: execvp() of './writefoo' failed: Operation not permitted` is
# the documented shape -- https://7402.org/blog/2020/macos-sandboxing-of-folder.html,
# read 2026-08-22). Neither of this repository's two confined children ever
# writes that prefix, so the test is specific.
SANDBOX_EXEC_DIAGNOSTIC_PREFIX: Final = "sandbox-exec:"


class Confinement:
    """One platform's answer to "deny every write except this one path"."""

    name: str = "no confinement"
    platform: str = "unknown"

    def __init__(self) -> None:
        self._launcher_path: str | None = None
        self._launcher_identity: tuple[int, int] | None = None

    def launcher(self, name: str, *, system_path: str) -> str:
        """Pin one fixed-system launcher for both probe and real child.

        A fallback through ``PATH`` lets an inherited environment choose the
        program that establishes the boundary. A resolved pathname alone is
        not a pin either: the directory entry can be replaced between the
        probe and the real child while retaining the same spelling. Require
        the reviewed absolute system location, refuse a symlink there, and
        compare its device/inode identity every time the command is built.
        """

        candidate = Path(system_path)
        if not candidate.is_absolute():
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=f"the reviewed {name} confinement launcher path is not absolute",
            )
        try:
            identity = candidate.stat(follow_symlinks=False)
        except OSError as error:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=f"this host has no trusted {name} confinement launcher at {candidate}",
            ) from error
        if not stat.S_ISREG(identity.st_mode):
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=f"the trusted {name} confinement launcher at {candidate} is not a regular file",
            )
        observed = (identity.st_dev, identity.st_ino)
        if self._launcher_path is None:
            self._launcher_path = str(candidate)
            self._launcher_identity = observed
        elif self._launcher_path != str(candidate) or self._launcher_identity != observed:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=(
                    f"the trusted {name} confinement launcher changed after the boundary "
                    "probe selected it; the real child was not started"
                ),
            )
        return self._launcher_path

    def command(self, command: Sequence[str], *, writable: Path | None = None) -> list[str]:
        """Wrap `command` so the OS denies every write except below `writable`."""
        raise NotImplementedError  # pragma: no cover

    def launcher_failure(self, completed: subprocess.CompletedProcess) -> str | None:
        """Name the boundary failure when the launcher never ran its target.

        Returns ``None`` when the nonzero exit came from the confined program
        itself, which is an ordinary verdict about the run tree or the advance
        request rather than a statement about the boundary.
        """
        return None

    def enforcement(self) -> str:
        """Plain language for what this backend does and does not enforce."""
        raise NotImplementedError  # pragma: no cover


class LandlockConfinement(Confinement):
    """Linux: util-linux ``setpriv``, which is how this repository reaches Landlock.

    Landlock is inherited across ``exec`` and cannot be relaxed by a child. The
    UI receives ``writable=None`` and cannot mutate any filesystem object. The
    custody worker receives only the receipt directory, which is the run-tree's
    append-only decision channel; stage artifacts, blobs, manifests, and run
    authority remain refused by the kernel.
    """

    name = "Linux Landlock (setpriv)"
    platform = "linux"

    def __init__(self) -> None:
        super().__init__()
        self._seccomp_filter_path: Path | None = None

    def command(self, command: Sequence[str], *, writable: Path | None = None) -> list[str]:
        tool = self.launcher("setpriv", system_path="/usr/bin/setpriv")
        result = [tool, "--no-new-privs", "--landlock-access", f"fs:{_WRITE_RIGHTS}"]
        if self._seccomp_filter_path is not None:
            result += ["--seccomp-filter", str(self._seccomp_filter_path)]
        if writable is not None:
            # ``RunTree._atomic_create`` creates a temporary then links it into
            # the content-addressed target, so this narrow allowance needs
            # creation, linking, and temporary cleanup — no stage directory is
            # included. ``setpriv`` splits this rule on its first two colons
            # only, so a run root containing a colon still names one path
            # (verified against util-linux 2.41).
            result += [
                "--landlock-rule",
                f"path-beneath:write-file,remove-file,make-reg,refer:{writable.resolve()}",
            ]
        return [*result, "--", *command]

    def launcher_failure(self, completed: subprocess.CompletedProcess) -> str | None:
        if completed.returncode != SETPRIV_PRIVILEGE_FAILURE_EXIT:
            return None
        return (
            "the Landlock launcher could not establish its boundary on this kernel, so the "
            "program it guards never ran: " + _diagnostic(completed)
        )

    def enforcement(self) -> str:
        return (
            "Landlock denies every filesystem mutation for this process and its children, "
            "except any single path explicitly allowed; the custody runner's inherited "
            "seccomp filter also denies socket creation and same-user process injection. "
            "Reads and process creation remain allowed"
        )


class SeatbeltConfinement(Confinement):
    """macOS: a Seatbelt profile applied by ``sandbox-exec``.

    The profile starts from ``deny default`` and grants only global file reads,
    exec of the exact Python binary being launched, and (for the worker) one
    writable subtree. Network, Mach service lookup, process inspection, and
    process creation are not granted. This avoids depending on disputed SBPL
    rule-precedence folklore to punch an allow through a blanket deny, and it
    means an accidentally omitted operation fails closed in the native probe.
    The profile is applied before ``execvp`` and remains attached across exec.

    This backend was exercised on macOS 15 (Darwin 24) through both confined
    children. The native run established that captured stdin/stdout remain
    usable, the framework interpreter's exact re-exec target is sufficient,
    and `verify_confinement` observes denied writes and outbound connections
    after the profile applies. The probe still runs before every launch: a
    past native pass is evidence about that host and moment, not permission to
    assume a later host enforces the same profile.
    """

    name = "macOS Seatbelt (sandbox-exec)"
    platform = "darwin"

    def command(self, command: Sequence[str], *, writable: Path | None = None) -> list[str]:
        try:
            tool = self.launcher("sandbox-exec", system_path="/usr/bin/sandbox-exec")
        except OperatorError as error:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=(
                    "this macOS host has no sandbox-exec, so the console's OS-enforced "
                    "no-write boundary cannot be established and the console will not run; "
                    "nothing here is enforced by convention instead"
                ),
            ) from error
        if not command or not Path(command[0]).is_absolute():
            # No `--` separator below, so the wrapped program must not be
            # readable as an option. Every call site passes `sys.executable`;
            # this refuses rather than assume how `sandbox-exec`'s option
            # parsing treats a separator that cannot be tested from here.
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail="the confined program must be named by an absolute path",
            )
        checked_command = [str(Path(command[0]).resolve()), *command[1:]]
        return [
            tool,
            "-p",
            self.profile(writable, executable=Path(checked_command[0])),
            *checked_command,
        ]

    def profile(self, writable: Path | None = None, *, executable: Path | None = None) -> str:
        """The SBPL text applied to the child, built from a checked path."""

        program = Path(sys.executable if executable is None else executable).resolve()
        lines = [
            "(version 1)",
            "(deny default)",
            # Measured on macOS 15 (Darwin 24): under this profile shape a
            # loopback socket still connected with only (deny default), so the
            # network family is denied by name rather than trusted to the
            # default. Explicit beats implicit where the probe proved implicit
            # insufficient.
            "(deny network*)",
            "(allow file-read*)",
            f'(allow process-exec (literal "{_sbpl_string(program)}"))',
        ]
        # A framework build of CPython is a stub: bin/python3.x posix_spawns the
        # framework's Resources/Python.app/Contents/MacOS/Python. Denying that
        # second exec kills the child before it runs ("posix_spawn ... Undefined
        # error: 0"), so the one re-exec target is allowed by literal path — the
        # same least-privilege shape as the stub itself, never a subpath.
        for parent in program.parents:
            if parent.name.startswith("3.") and parent.parent.name == "Versions":
                framework_binary = (
                    parent / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
                )
                if framework_binary.is_file():
                    lines.append(
                        f'(allow process-exec (literal "{_sbpl_string(framework_binary)}"))'
                    )
                break
        if writable is not None:
            # No trailing slash: `subpath` matches the directory and everything
            # beneath it, and a trailing separator is the documented way to
            # make such a rule quietly match nothing.
            lines.append(f'(allow file-write* (subpath "{_sbpl_string(writable.resolve())}"))')
        return "\n".join(lines) + "\n"

    def launcher_failure(self, completed: subprocess.CompletedProcess) -> str | None:
        text = completed.stderr or ""
        if not any(line.startswith(SANDBOX_EXEC_DIAGNOSTIC_PREFIX) for line in text.splitlines()):
            return None
        return (
            "the Seatbelt launcher could not establish its boundary on this host, so the "
            "program it guards never ran: " + _diagnostic(completed)
        )

    def enforcement(self) -> str:
        return (
            "a deny-default Seatbelt profile permits filesystem reads and the exact Python "
            "exec, plus any single writable subtree explicitly allowed; it denies network, "
            "Mach service lookup, process inspection, and process creation"
        )


class NoConfinement(Confinement):
    """A platform this repository has no OS-level boundary for.

    Absence is stated rather than tolerated. The in-process import boundary
    still holds on such a host — the renderer genuinely cannot reach a writer —
    but "the code does not try" is not the claim this unit makes, so the
    console refuses to open rather than present an unenforced boundary as an
    enforced one.
    """

    def __init__(self, platform: str):
        super().__init__()
        self.platform = platform
        self.name = f"no confinement backend for {platform!r}"

    def command(self, command: Sequence[str], *, writable: Path | None = None) -> list[str]:
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=self.enforcement(),
        )

    def enforcement(self) -> str:
        return (
            f"this platform is {self.platform!r}, and Verbatus has no OS-enforced write "
            "boundary for it: Landlock is Linux-only and Seatbelt is macOS-only. What is "
            "still enforced is only that the console's own code holds no writer and no "
            "provider credential. What is NOT enforced is the operating system's refusal, "
            "so a compromised console could reach evidence — and the console therefore "
            "will not open here"
        )


_BACKENDS: Final = {"linux": LandlockConfinement, "darwin": SeatbeltConfinement}


def confinement(platform: str | None = None) -> Confinement:
    """Pick this host's confinement backend, naming absence rather than hiding it."""

    name = sys.platform if platform is None else platform
    backend = _BACKENDS.get(name)
    return backend() if backend is not None else NoConfinement(name)


def _sbpl_string(path: Path) -> str:
    """Quote one path into an SBPL string literal, refusing anything unquotable.

    A run root is operator-supplied and reaches the child as *policy text*
    rather than as an argument vector, so a stray quote would not corrupt an
    argument — it would end the string and let the rest of the path be read as
    profile syntax. Backslash and quote are escaped; a control character is
    refused outright, because there is no legitimate run root containing one
    and a newline is exactly how an injected rule would be introduced.
    """

    text = str(path)
    if any(character < " " or character == "\x7f" for character in text):
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=(
                "the run tree's decision-record path contains a control character, so no "
                "sandbox profile can name it safely; move the run root to an ordinary path"
            ),
        )
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _diagnostic(completed: subprocess.CompletedProcess) -> str:
    return (completed.stderr or completed.stdout or "").strip() or "no diagnostic"


# The probe must distinguish three outcomes that an exit code alone cannot: mutation
# and delegation were refused (the boundary holds), either succeeded (the
# boundary is incomplete), and nothing ran at all (the launcher failed). Only the first is a
# pass, so a launcher that silently declines to confine cannot be mistaken for
# a kernel that refused the write.
# The network half probes an outbound connect, not bare socket creation:
# macOS Seatbelt mediates `network-outbound` while socket() itself succeeds
# under (deny default), so a creation-only probe reports the sandbox absent
# where it is in force. A denial surfaces as EPERM/EACCES from the kernel;
# ECONNREFUSED or a timeout means the connect reached the network stack,
# which is exactly the capability the boundary must not grant.
_PROBE_SOURCE: Final = (
    "import errno, socket, sys\n"
    "from pathlib import Path\n"
    "try:\n"
    "    Path(sys.argv[1]).write_text('probe')\n"
    "except OSError:\n"
    "    print('WRITE_REFUSED')\n"
    "else:\n"
    "    print('WRITE_PERMITTED')\n"
    "try:\n"
    "    connection = socket.socket()\n"
    "except OSError:\n"
    "    print('NETWORK_REFUSED')\n"
    "else:\n"
    "    try:\n"
    "        connection.settimeout(0.5)\n"
    "        connection.connect(('127.0.0.1', 9))\n"
    "    except OSError as error:\n"
    "        if error.errno in (errno.EPERM, errno.EACCES):\n"
    "            print('NETWORK_REFUSED')\n"
    "        else:\n"
    "            print('NETWORK_PERMITTED')\n"
    "    else:\n"
    "        print('NETWORK_PERMITTED')\n"
    "    finally:\n"
    "        connection.close()\n"
)
_PROBE_REFUSED: Final = "WRITE_REFUSED\nNETWORK_REFUSED"
_PROBE_WRITE_PERMITTED: Final = "WRITE_PERMITTED"
_PROBE_NETWORK_PERMITTED: Final = "NETWORK_PERMITTED"


def verify_confinement(
    backend: Confinement,
    *,
    writable: Path | None,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Make the backend deny a real write and socket before either child launches.

    Deliberately re-run per launch rather than cached: the probe costs one
    short subprocess on a human-paced command, and a cached "yes" is a claim
    about a moment that has passed. It exercises the *same* profile the real
    launch will use, allowance included, so a malformed writable path is
    caught here rather than at the point it would have widened the boundary.
    """

    with tempfile.TemporaryDirectory(prefix="verbatus-custody-probe-") as scratch:
        target = Path(scratch).resolve() / "write-that-must-be-refused"
        if writable is not None and target.is_relative_to(writable.resolve()):
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=(
                    "the confinement probe's target lies inside the one permitted path, so "
                    "it could not have tested the boundary"
                ),
            )
        command = backend.command(
            [
                str(Path(sys.executable).resolve()),
                *CHILD_INTERPRETER_FLAGS,
                "-c",
                _PROBE_SOURCE,
                str(target),
            ],
            writable=writable,
        )
        completed = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True, check=False
        )
        verdict = (completed.stdout or "").strip()
        if completed.returncode == 0 and verdict == _PROBE_REFUSED:
            return
        if _PROBE_WRITE_PERMITTED in verdict or _PROBE_NETWORK_PERMITTED in verdict:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=(
                    f"{backend.name} did not refuse both an outside write and network access, "
                    "so the boundary this console depends on is not in force: "
                    + backend.enforcement()
                ),
            )
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=(
                f"{backend.name} could not be established, so nothing ran inside it: "
                + (backend.launcher_failure(completed) or _diagnostic(completed))
            ),
        )


# Linux exposes a same-user process's original environment through
# `/proc/<pid>/environ`, even when the child itself was launched with `env={}`.
# Provider credentials normally enter the operator in that original mapping,
# so scrubbing only the child's mapping did not satisfy the compromised-UI
# claim. PR_SET_DUMPABLE=0 makes the kernel's ptrace access check refuse that
# read while the child exists. The prior value is restored after wait() so this
# narrow command does not silently change the operator process for its lifetime.
_PR_GET_DUMPABLE: Final = 3
_PR_SET_DUMPABLE: Final = 4

# `setpriv --seccomp-filter` consumes raw native `struct sock_filter` records.
# This deliberately small cBPF program permits the ordinary Python syscall set
# and returns EPERM only for routes that can delegate around Landlock: creating
# a socket, injecting into another same-user process, or opening an io_uring
# that could perform socket operations. The architecture check kills a compat
# executable rather than interpreting its syscall numbers under the native map.
_BPF_LD_W_ABS: Final = 0x20
_BPF_JMP_JEQ_K: Final = 0x15
_BPF_JMP_JGE_K: Final = 0x35
_BPF_RET_K: Final = 0x06
_SECCOMP_RET_KILL_PROCESS: Final = 0x80000000
_SECCOMP_RET_ERRNO: Final = 0x00050000
_SECCOMP_RET_ALLOW: Final = 0x7FFF0000
_SECCOMP_ARCH_AND_SYSCALLS: Final = {
    "x86_64": (
        0xC000003E,
        (41, 53, 101, 298, 310, 311, 321, 323, 425, 438),
    ),
    "aarch64": (
        0xC00000B7,
        (117, 198, 199, 241, 270, 271, 280, 282, 425, 438),
    ),
    "arm64": (
        0xC00000B7,
        (117, 198, 199, 241, 270, 271, 280, 282, 425, 438),
    ),
}


def _seccomp_filter_bytes() -> bytes:
    machine = host_platform.machine().lower()
    facts = _SECCOMP_ARCH_AND_SYSCALLS.get(machine)
    if facts is None:
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=f"Linux architecture {machine!r} has no reviewed custody seccomp filter",
        )
    audit_arch, refused_syscalls = facts
    instructions = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # seccomp_data.arch
        (_BPF_JMP_JEQ_K, 1, 0, audit_arch),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, 0),  # seccomp_data.nr
    ]
    if machine == "x86_64":
        # An x32-ABI syscall reports AUDIT_ARCH_X86_64 with nr | 0x40000000, so
        # every exact-number comparison below would miss it and fall through to
        # ALLOW. Kill the whole x32 range instead of
        # translating the table: nothing this worker runs is an x32 binary.
        instructions.extend(
            (
                (_BPF_JMP_JGE_K, 0, 1, 0x40000000),
                (_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
            )
        )
    for number in refused_syscalls:
        instructions.extend(
            (
                (_BPF_JMP_JEQ_K, 0, 1, number),
                (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EPERM),
            )
        )
    instructions.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW))
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in instructions)


@contextmanager
def _delegation_boundary(backend: Confinement) -> Iterator[None]:
    """The filter path must remain live across both the probe and real exec."""

    if not isinstance(backend, LandlockConfinement):
        yield
        return
    with tempfile.NamedTemporaryFile(prefix="verbatus-custody-seccomp-") as handle:
        handle.write(_seccomp_filter_bytes())
        handle.flush()
        backend._seccomp_filter_path = Path(handle.name)
        try:
            yield
        finally:
            backend._seccomp_filter_path = None


@contextmanager
def _launcher_environment_hidden(backend: Confinement) -> Iterator[None]:
    if backend.platform != "linux":
        yield
        return
    # Imported here, not at module top: under the macOS Seatbelt profile the
    # confined advance worker imports this module, and `import ctypes` aborts
    # the child outright (libffi's executable trampolines are exactly what the
    # profile denies). Only this Linux-specific branch needs it.
    from ctypes import CDLL, get_errno

    libc = CDLL(None, use_errno=True)
    previous = libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)
    if previous < 0 or libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=(
                "Linux could not hide the credential-bearing launcher environment from its "
                f"confined child (prctl errno {get_errno()})"
            ),
        )
    try:
        yield
    finally:
        if libc.prctl(_PR_SET_DUMPABLE, previous, 0, 0, 0) != 0:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=f"Linux could not restore launcher dumpability (prctl errno {get_errno()})",
            )


def run_confined(
    command: Sequence[str],
    *,
    writable: Path | None,
    cwd: Path,
    input_text: str,
) -> tuple[Confinement, subprocess.CompletedProcess[str]]:
    """Verify and run one child through the same pinned custody facts.

    The backend instance, launcher binary, Python binary, environment, cwd, and
    writable allowance are shared by preflight and real launch. Keeping this in
    one function prevents either caller from accidentally probing one boundary
    and executing through another.
    """

    interpreter = str(Path(sys.executable).resolve())
    if not command or Path(command[0]).resolve() != Path(interpreter):
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail="the confined child is not the same checked Python executable as its probe",
        )
    checked_command = [interpreter, *command[1:]]
    environment = custody_environment()
    require_no_provider_credentials(environment)
    backend = confinement()
    checked_cwd = cwd.resolve()
    checked_writable = None
    writable_identity = None
    if writable is not None:
        lexical_identity = _plain_directory_identity(writable, "the confined writable directory")
        checked_writable = writable.resolve()
        writable_identity = _plain_directory_identity(
            checked_writable, "the confined writable directory"
        )
        if lexical_identity != writable_identity:
            raise OperatorError(
                ErrorCode.CONSOLE_CUSTODY_REFUSED,
                detail=(
                    "the confined writable directory reaches a different device or inode "
                    "after path resolution; symlinked write allowances are refused"
                ),
            )
    with _delegation_boundary(backend):
        verify_confinement(
            backend, writable=checked_writable, cwd=checked_cwd, environment=environment
        )
        if checked_writable is not None:
            _require_directory_identity(checked_writable, writable_identity)
        wrapped = backend.command(checked_command, writable=checked_writable)
        if checked_writable is not None:
            # ``command`` constructs policy text and is an extension seam. It
            # must not be able to swap the allowed path after the preflight
            # identity check and before the subprocess consumes that policy.
            _require_directory_identity(checked_writable, writable_identity)
        with _launcher_environment_hidden(backend):
            completed = subprocess.run(
                wrapped,
                cwd=checked_cwd,
                env=environment,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
            )
        if checked_writable is not None:
            _require_directory_identity(checked_writable, writable_identity)
    return backend, completed


def _plain_directory_identity(path: Path, label: str) -> tuple[int, int]:
    """Return a no-follow directory identity suitable for later comparison."""

    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as error:
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=f"{label} could not be inspected: {error}"
        ) from error
    if not stat.S_ISDIR(observed.st_mode):
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=f"{label} is not a real directory; a symlink is not a writable boundary",
        )
    return observed.st_dev, observed.st_ino


def _require_directory_identity(path: Path, expected: tuple[int, int] | None) -> None:
    """Refuse a writable allowance whose directory entry changed after review."""

    if (
        expected is None
        or _plain_directory_identity(path, "the confined writable directory") != expected
    ):
        raise OperatorError(
            ErrorCode.CONSOLE_CUSTODY_REFUSED,
            detail=(
                "the confined writable directory changed device or inode between the "
                "boundary probe and use; the child result is not trusted"
            ),
        )


def landlock_command(command: Sequence[str], *, writable: Path | None = None) -> list[str]:
    """Tests pin this compatibility name as part of the Linux boundary surface."""

    return LandlockConfinement().command(command, writable=writable)
