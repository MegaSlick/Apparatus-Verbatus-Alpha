"""`close --lease <id>`: the verb a live pod could not be closed by.

Before this, `operations/pod/cli.py` offered `create` and `adopt` and nothing
else. A pod that was already running could be stopped only by its sealed hard
lifetime, by a supervisor tick that happened to observe a non-`RUNNING`
provider state, or by the provider's own console -- and the operator surface's
`close` is fixture-only, so it could not touch a real one. On the path
GOVERNANCE 8 exists for, that is the wrong set of options.

The drills here are `test_launch_drill.py`'s: one real `PodRuntime.create`
through the real armer against `FakeProvider`, a directory standing in for the
volume, a fake clock for every sleep. What is added is the close afterwards --
`supervise.close_lease_now`, which drives `_close_lease`, the same function
`supervise_tick` drives on an `EXITED` pod, so the verification standard is
`VerifiedShutdown`'s one standard and not a second implementation of it.

Nothing here reaches a network, a provider account, or money.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable, Iterator

import pytest

from . import cli, notify_hooks, supervise
from .fake_provider import FakeProvider
from .launch import LaunchState
from .notify_bridge import NotifyOutcome
from .shutdown import CloseState, VerifiedShutdown
from .test_launch_drill import LEASE_ID, SPEND_TOML, Drill, closer
from .test_launch_drill import _build as build_one_drill


@pytest.fixture
def build_drill(tmp_path: Path) -> Iterator[Callable[..., Drill]]:
    """`test_launch_drill`'s own fixture, re-declared over the same builder.

    Importing the fixture itself would leave every test's `build_drill`
    parameter shadowing the imported name; repeating four lines is cheaper than
    silencing that on every test in the file. The teardown is the imported
    module's and matters as much here: `establish_identity` holds a lease's
    kernel lock for the life of the process, so a drill that never released it
    would leave the lock held for a lease nothing is supervising.
    """

    made: list[Drill] = []

    def build(**kwargs: object) -> Drill:
        drill, _ = build_one_drill(tmp_path, **kwargs)  # type: ignore[arg-type]
        made.append(drill)
        return drill

    yield build
    for drill in made:
        drill.starter.stop_all()


def live_drill(build: Callable[..., Drill]) -> Drill:
    """One green launch: a real pod, a durable lease, a supervisor holding it."""

    drill = build(lifetime=300)
    drill.pod_writes_at(2)
    result = drill.launch()
    assert result.state is LaunchState.CREATED_GUARDED, result.detail
    return drill


def run_close(
    drill: Drill,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    lease: str = LEASE_ID,
    provider_name: str = "fake",
    spend: Path | None = None,
    notify: bool = False,
    provider: object | None = None,
    record_fixture: Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Drive the real `cli.main` close, with the provider factory and clock replaced.

    Deliberately passes no `--controller-armer-factory`: a close arms nothing,
    and an operator reaching for this verb in the moment a pod is billing must
    not have to name an untracked factory it would never call.

    The shutdown controller is rebuilt on the drill's own clock -- with the
    timings `cli._close_command` derived from the spend policy, untouched. The
    whole drill runs on one fake clock, and the fake provider stamps its billing
    evidence on that clock, so a close driven off the wall clock would be
    comparing two timelines that were never the same one. It also keeps the
    poll and billing-retry tails from costing real seconds.
    """

    acting = provider if provider is not None else drill.provider
    monkeypatch.setattr(cli, "_provider", lambda _reference: acting)

    def on_the_drills_clock(provider: object, **timings: object) -> VerifiedShutdown:
        return VerifiedShutdown(
            provider,  # type: ignore[arg-type]
            monotonic=drill.clock.monotonic,
            sleeper=drill.clock.sleep,
            now=drill.clock.now,
            **timings,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(cli, "VerifiedShutdown", on_the_drills_clock)
    argv = [
        "--provider-factory",
        "unused:factory",
        "--spend",
        str(spend or tmp_path / "spend.toml"),
        "--leases",
        str(drill.lease_root),
        "--provider-name",
        provider_name,
    ]
    if notify:
        argv.append("--notify")
    if record_fixture is not None:
        argv += ["--record-fixture", str(record_fixture)]
    argv += ["close", "--lease", lease]
    exit_code = cli.main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def test_a_close_verifies_the_pod_is_gone_and_writes_the_terminal_lease(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The green path, end to end through `cli.main`.

    All three observations the close report requires are made against the fake:
    exact-pod GET absence, independent pod-list absence, and non-empty exact-pod
    billing through the cutoff. The lease reaches `closed-verified`, and the
    close line reaches the phone through the same hook `create` uses.
    """

    drill = live_drill(build_drill)
    pod_id = drill.pod_id()
    # The supervisor this launch armed has stopped -- the ordinary case for an
    # operator-driven close, and what releases the lease's kernel lock.
    drill.starter.stop_all()
    sent: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        notify_hooks,
        "notify_close",
        lambda *, lease_id, verified_state, billed_seconds, **_: (
            sent.append((lease_id, verified_state, billed_seconds)),
            NotifyOutcome(True, True, "test sink"),
        )[1],
    )

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, notify=True)

    assert exit_code == 0
    assert record["state"] == supervise.OPERATOR_CLOSE
    assert record["green"] is True
    assert record["lease_phase"] == "closed-verified"
    close = record["close"]
    assert isinstance(close, dict)
    assert close["pod_id"] == pod_id
    assert close["state"] == CloseState.VERIFIED.value
    assert close["pod_get_absent"] is True and close["pod_list_absent"] is True
    assert "UNVERIFIED" not in json.dumps(record)
    # The pod really was terminated and independently observed absent.
    assert drill.provider.terminate_calls == [pod_id]
    verbs = [verb for verb, _ in drill.provider.calls]
    assert "verify_absent" in verbs and "capture_cost" in verbs
    # The same close line `create` sends when it closes its own pod.
    assert sent == [(LEASE_ID, CloseState.VERIFIED.value, sent[0][2])]
    assert record["close_notification"] == "Phone notification: sent."

    # The durable lease says the same thing the record does.
    lease = drill.store.load()
    assert lease is not None and lease.phase == "closed-verified" and not lease.active


def test_an_unverified_close_keeps_the_lease_a_live_liability(
    build_drill: Callable[..., Drill],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Billing that cannot be captured is `UNVERIFIED`, and never zero.

    Driven at `close_lease_now` rather than through `cli.main` only so the
    shutdown controller's retry tail runs on the drill's fake clock instead of
    thirty real seconds; the close path itself is identical.

    The point of the assertion at the end: an unverified close does not release
    the operator. The lease stays short of `closed-verified`, so the
    single-live-pod invariant still refuses the next create -- exactly as it
    would while the pod was up.
    """

    del capsys
    drill = live_drill(build_drill)
    pod_id = drill.pod_id()
    drill.starter.stop_all()
    # The provider stops being able to account for this pod's billing, which is
    # the shape a close must never round down to "nothing was spent".
    drill.provider._billing.pop(pod_id)

    result, exit_code = supervise.close_lease_now(
        store=drill.store,
        leases_root=drill.lease_root,
        lease_id=LEASE_ID,
        provider_name="fake",
        shutdown=closer(drill.provider, drill.clock),
        reason="drill: an operator asked for an immediate close",
    )

    assert exit_code == 3, "an unverified close must never exit as a guarded success"
    assert result.state == supervise.OPERATOR_CLOSE
    assert result.green is False
    assert result.close_report is not None
    assert result.close_report.verified is False
    assert result.close_report.state is not CloseState.VERIFIED
    assert result.close_report.cost_capture is not None
    assert result.close_report.cost_capture.total_usd is None, "zero was not inferred"

    lease = drill.store.load()
    assert lease is not None and lease.phase == "close-unverified"
    # The other half of `_absence_before_any_terminate`'s rule: a lease reaches
    # `close-unverified` only when a terminate was actually issued against the
    # pod. Here one was, repeatedly, and the phase is earned.
    assert drill.provider.terminate_calls == [pod_id]

    # Still a liability: the next create refuses on it. Through the balance
    # floor rather than the single-live-pod check, because an unverified close
    # leaves the remaining liability *unknowable* rather than merely open --
    # which is the stronger of the two refusals and the honest one here.
    refused = drill.runtime.create(drill.ask, confirmation=None)
    assert refused.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE
    assert "unverified close" in refused.detail


def test_the_cli_says_unverified_close_and_does_not_exit_zero(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The word the operator surface reserves for this reaches this surface too.

    Here the pod refuses to disappear, so neither absence observation is ever
    made and the close ends `UNVERIFIED` on its own deadline -- the real
    `VerifiedShutdown`, on a one-second reviewed deadline supplied by the spend
    policy this close is run under, rather than a stubbed one.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    drill.provider.set_disappearance_lag(drill.pod_id(), get_polls=99, list_polls=99)
    impatient = tmp_path / "impatient-spend.toml"
    impatient.write_text(
        SPEND_TOML.replace("shutdown_deadline_seconds = 8", "shutdown_deadline_seconds = 1"),
        encoding="utf-8",
    )

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, spend=impatient)

    assert exit_code == 3, "an unverified close must never exit as a guarded success"
    assert record["green"] is False
    assert str(record["detail"]).startswith("UNVERIFIED CLOSE: ")
    close = record["close"]
    assert isinstance(close, dict)
    assert close["state"] != CloseState.VERIFIED.value
    assert close["pod_get_absent"] is False
    # The terminate was still issued, repeatedly: the pod is the thing that did
    # not go away, and the report says so rather than rounding it to a success.
    assert drill.provider.terminate_calls, "no termination was attempted"
    assert record["lease_phase"] == "close-unverified"


def test_close_refuses_a_lease_this_account_does_not_hold(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown lease id, and a lease armed under another provider account.

    Both refuse before any provider call: exit 2 is this surface's "nothing was
    touched", and a close that reached for a stranger's pod would be the worse
    failure of the two.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, lease="f" * 32)
    assert exit_code == 2
    assert record["state"] == supervise.LEASE_NOT_HELD
    assert "f" * 32 in str(record["detail"])
    assert record["close"] is None

    exit_code, record = run_close(
        drill, tmp_path, monkeypatch, capsys, provider_name="someone-elses-account"
    )
    assert exit_code == 2
    assert record["state"] == supervise.LEASE_NOT_HELD
    assert "someone-elses-account" in str(record["detail"])

    assert drill.provider.terminate_calls == [], "a refused close touched a pod"
    lease = drill.store.load()
    assert lease is not None and lease.active


def test_close_refuses_while_a_live_supervisor_holds_the_lease(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two closers over one pod is the failure the lease lock exists to stop.

    The supervisor armed by the launch is still holding the lock here, so the
    verb refuses and says which lock and what to do -- rather than racing the
    process that is already guarding the pod.
    """

    drill = live_drill(build_drill)

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys)

    assert exit_code == 2
    assert record["state"] == supervise.SUPERVISOR_BUSY
    assert "stop it first" in str(record["detail"])
    assert drill.provider.terminate_calls == []
    lease = drill.store.load()
    assert lease is not None and lease.active


def test_a_lease_already_closed_is_reported_rather_than_closed_again(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The second close makes no provider call at all."""

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    first, _ = run_close(drill, tmp_path, monkeypatch, capsys)
    assert first == 0
    terminations = list(drill.provider.terminate_calls)

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys)

    assert exit_code == 0
    assert record["state"] == "closed-verified"
    assert "no provider call was made" in str(record["detail"])
    assert drill.provider.terminate_calls == terminations


def test_close_refuses_an_unconfigured_spend_policy(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a spend gate -- a timings gate.

    The shutdown controller's poll interval, deadline and billing-cutoff margin
    are reviewed policy values. A close driven on invented timings is not the
    close this repository verifies, so it refuses and says so, touching nothing.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    unconfigured = tmp_path / "unconfigured.toml"
    unconfigured.write_text('schema = "pod-spend.v3"\nstate = "unconfigured"\n', encoding="utf-8")

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, spend=unconfigured)

    assert exit_code == 2
    assert record["state"] == "refused"
    assert "unconfigured" in str(record["detail"])
    assert drill.provider.terminate_calls == []


def final_records(lease_root: Path) -> list[dict[str, object]]:
    """Every durable per-run record this lease root holds, oldest name first."""

    directory = lease_root / "supervisors"
    if not directory.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("supervisor-*final*.json"))
    ]


def test_a_lease_file_that_cannot_be_read_is_go_and_look_not_nothing_paid(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A damaged lease is exit 3, and it names the file.

    Exit 2 on this path said "refused, and nothing was paid" about a lease
    record that exists, was written by a paid action, and could not be opened --
    the pod it names may be billing at this moment. The only honest status is
    "go and look", and the only useful detail is the exact path to look at.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    drill.store.path.write_text("{ this is not a lease", encoding="utf-8")

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys)

    assert exit_code == 3, "an unreadable lease is not 'nothing was paid'"
    assert record["state"] == "refused"
    assert str(drill.store.path) in str(record["detail"])
    assert "go and look" in str(record["detail"])
    assert drill.provider.terminate_calls == []


def test_a_lease_file_that_names_another_lease_is_refused(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`<root>/<id>.json` holding a different `lease_id` closes nothing.

    The drill is the literal one: the lease file is copied to a second name, so
    the root holds one live pod's record under two ids. Closing on the copy
    would terminate the pod under the id the operator typed while
    `LeaseStore.record_close` wrote the outcome into the identity the file
    actually carries -- two lease identities over one pod, and no way to tell
    afterwards which was which. Exit 3, because a live pod is involved.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    renamed = "c" * 32
    shutil.copyfile(drill.store.path, drill.lease_root / f"{renamed}.json")

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, lease=renamed)

    assert exit_code == 3
    assert record["state"] == "refused"
    assert renamed in str(record["detail"]) and LEASE_ID in str(record["detail"])
    assert drill.provider.terminate_calls == []
    # The original is untouched, and still active for its supervisor.
    lease = drill.store.load()
    assert lease is not None and lease.active


def test_a_lease_id_that_is_not_a_lease_id_builds_no_path_at_all(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--lease ../../elsewhere` is refused before a path is built from it.

    The id is interpolated into four paths -- the lease file, the kernel lock,
    the identity file and the durable final record -- so it is checked before
    the first of them exists. The record this refusal files is the anonymous
    one, which is the assertion that the traversal reached no filename.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, lease="../../etc/passwd")

    assert exit_code == 2
    assert record["state"] == "refused"
    assert "32 lowercase hexadecimal" in str(record["detail"])
    assert drill.provider.terminate_calls == []
    written = Path(str(record["final_record"]))
    assert written.parent == drill.lease_root / "supervisors"
    assert written.name.startswith("supervisor-final-"), "the bad id reached a filename"
    assert not (drill.lease_root.parent.parent / "etc").exists()
    # The real lease is untouched and still guarded.
    lease = drill.store.load()
    assert lease is not None and lease.active


def test_a_pod_absent_before_any_terminate_refuses_and_leaves_the_lease_armed(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wrong `--provider-factory`, and why it must not write a terminal phase.

    `--provider-name` is a label recorded in the lease; nothing reconciles it
    against the credentials behind `--provider-factory`. So a factory pointing
    at another account passes every holding check, and the close would then
    terminate nothing, observe a genuine absence and no billing there, and
    write `close-unverified` -- which is not `PodLease.active`, so
    `run_supervisor` would stop guarding a pod that is still running and still
    billing on the real account.

    The provider here is a second fake that never saw this pod, which is
    exactly what a wrong account looks like from this side.
    """

    drill = live_drill(build_drill)
    pod_id = drill.pod_id()
    drill.starter.stop_all()
    stranger = FakeProvider()

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, provider=stranger)

    assert exit_code == 3, "a pod that may be billing elsewhere is not 'nothing was paid'"
    assert record["state"] == supervise.POD_ABSENT_UNCLOSED
    assert record["green"] is False
    assert record["close"] is None
    assert "--provider-factory" in str(record["detail"])
    assert pod_id in str(record["detail"])
    # Nothing was terminated, on either account.
    assert stranger.terminate_calls == []
    assert drill.provider.terminate_calls == []
    # And the lease is exactly as it was: still active, so a supervisor started
    # now takes it back and keeps guarding the pod.
    lease = drill.store.load()
    assert lease is not None and lease.active and lease.phase == "active"


def test_a_second_close_on_an_unverified_lease_still_says_unverified(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The already-terminal branch carries the word too.

    This is the operator coming back to ask whether the earlier close finished.
    It makes no provider call, so it has no close report to be judged by -- and
    a record that merely said "lease already reached terminal phase" would read
    as done.
    """

    drill = live_drill(build_drill)
    pod_id = drill.pod_id()
    drill.starter.stop_all()
    drill.provider._billing.pop(pod_id)
    first_code, first = run_close(drill, tmp_path, monkeypatch, capsys)
    assert first_code == 3 and first["lease_phase"] == "close-unverified"

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys)

    assert exit_code == 3
    assert record["state"] == "close-unverified"
    assert str(record["detail"]).startswith("UNVERIFIED CLOSE: ")
    assert record["close"] is None
    assert "no provider call was made" in str(record["detail"])


def test_every_close_refusal_leaves_a_durable_record(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GOVERNANCE 2, on the verb most likely to be run into a closing terminal.

    `supervise.main` has filed one record per run since it landed; this is the
    same file, from the same function, for the other driver of the same close
    path. The refusal drilled here is the unconfigured spend policy, which
    reaches no provider at all -- so the record is the *only* trace it left.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    unconfigured = tmp_path / "unconfigured.toml"
    unconfigured.write_text('schema = "pod-spend.v3"\nstate = "unconfigured"\n', encoding="utf-8")

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, spend=unconfigured)

    assert exit_code == 2
    filed = final_records(drill.lease_root)
    assert len(filed) == 1, filed
    assert filed[0]["lease_id"] == LEASE_ID
    assert filed[0]["exit_code"] == 2
    assert filed[0]["state"] == "refused"
    assert "unconfigured" in str(filed[0]["detail"])
    assert record["final_record"] == str(
        drill.lease_root / "supervisors" / Path(str(record["final_record"])).name
    )


def test_the_close_record_says_what_the_phone_hook_did(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--notify` wires balance notifications for a close too, and says so.

    A close consults no ceiling, but the hook is attached to the provider
    before this verb is dispatched, and a vendor adapter that observes its
    account balance while terminating or capturing cost pages the phone through
    it. Whether that wiring took, and what each ping did, is a fact about this
    close and belongs in its record rather than in a return value nobody reads.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    monkeypatch.setattr(
        notify_hooks,
        "notify_close",
        lambda **_: NotifyOutcome(True, True, "test sink"),
    )

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, notify=True)

    assert exit_code == 0
    wiring = record["balance_notification"]
    assert isinstance(wiring, dict)
    assert "wired" in str(wiring["wiring"])
    assert wiring["sent"] == []


def test_close_is_not_refused_by_a_provider_that_cannot_record_its_exchanges(
    build_drill: Callable[..., Drill],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create` refuses the flag it cannot honour; `close` files the fact and closes.

    The verb exists for the moment a pod is billing and something has already
    gone wrong. Trading a stopped meter for a fixture nobody asked for in that
    moment is the wrong way round -- so the unhonoured flag is recorded in the
    close record (GOVERNANCE 2) rather than raised as a refusal.
    """

    drill = live_drill(build_drill)
    drill.starter.stop_all()
    fixture_path = tmp_path / "never-written.jsonl"

    exit_code, record = run_close(drill, tmp_path, monkeypatch, capsys, record_fixture=fixture_path)

    assert exit_code == 0, "a close was refused over an evidence recorder"
    assert record["lease_phase"] == "closed-verified"
    assert "--record-fixture was not honoured" in str(record["record_fixture"])
    assert type(drill.provider).__name__ in str(record["record_fixture"])
    assert not fixture_path.exists()
