#!/usr/bin/env python3
"""Session-only RunPod v2 controller for the RecordGold four-page trial.

This is an operator helper, not the repository's managed pod runtime.  Importing it and
the ``prepare`` and ``preview`` commands perform no network or paid action.  A volume or
pod POST is reachable only through ``launch`` with a current exact-session authorization,
the literal execution flag, and a separately running lock-owning watchdog.

Provider credentials are loaded only inside commands that need the provider.  They are
never put in argv, durable records, error messages, or captured response bodies.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import tomllib
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid


API_ROOT = "https://api.runpod.io/v2/"
USER_AGENT = "verbatus-readiness/1.0"
PINNED_IMAGE = (
    "runpod/pytorch@sha256:"
    "0a360022e8de4375af99430f84e8b38951acc397252163a37ceac7204d01be35"
)
PINNED_IMAGE_CONFIG = (
    "sha256:02b731f844d2fbc4dd0de87253081363864327c7931b1ae2b5daa60abc600c51"
)
PINNED_ENTRYPOINT = ["/opt/nvidia/nvidia_entrypoint.sh"]
DEFAULT_STATE_ROOT = Path.home() / ".local/state/verbatus/runpod-live-trial-20260922"
MAX_TOTAL_SECONDS = 2 * 60 * 60
WATCHDOG_READY_MAX_AGE_SECONDS = 30
PROVISIONING_TIMEOUT_SECONDS = 20 * 60
POST_DELETE_BILLING_WAIT_SECONDS = 30 * 60
SCHEMA_SESSION = "verbatus-runpod-live-session.v1"
SCHEMA_AUTHORIZATION = "verbatus-runpod-live-authorization.v1"
SCHEMA_RUNTIME_RECEIPT = "verbatus-pod-runtime-receipt.v1"
SCHEMA_CONTROLLER_ACK = "verbatus-controller-ack.v1"


class Refusal(RuntimeError):
    """Fail-closed local or provider-contract refusal."""


class TransportUncertain(RuntimeError):
    """The provider may have received a request, but no response was established."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise Refusal(f"{label} must be a non-empty RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Refusal(f"{label} is not an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise Refusal(f"{label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise Refusal(f"{label} must be a decimal number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise Refusal(f"{label} must be a decimal number") from error
    if not result.is_finite() or result < 0:
        raise Refusal(f"{label} must be finite and non-negative")
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_write(path: Path, value: object, *, exclusive: bool = False) -> None:
    """Write mode-0600 JSON and fsync both the file and containing directory."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    data = canonical_bytes(value)
    if exclusive:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    else:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    fsync_directory(path.parent)


def durable_touch(path: Path) -> None:
    durable_write(path, {"requested_at": iso(utc_now())}, exclusive=not path.exists())


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal(f"cannot read {label} at {path}: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise Refusal(f"{label} at {path} is not a JSON object")
    return value


def event(session_dir: Path, kind: str, **fields: object) -> None:
    """Append a credential-free durable event. Callers must pass only public fields."""

    forbidden = ("api_key", "authorization", "secret", "token")
    for key in fields:
        if any(word in key.lower() for word in forbidden):
            raise Refusal(f"refusing to record credential-shaped event field {key!r}")
    row = {"at": iso(utc_now()), "event": kind, **fields}
    path = session_dir / "events.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, canonical_bytes(row))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(session_dir)


@contextlib.contextmanager
def exclusive_lock(path: Path, *, blocking: bool) -> Iterator[int]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(descriptor, flags)
        yield descriptor
    finally:
        os.close(descriptor)


def prepare_session(state_root: Path, now: dt.datetime | None = None) -> Path:
    now = now or utc_now()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_root, 0o700)
    session_id = f"recordgold-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:16]}"
    session_dir = state_root / session_id
    session_dir.mkdir(mode=0o700)
    identity = {
        "schema": SCHEMA_SESSION,
        "session_id": session_id,
        "created_at": iso(now),
        "pod_name": f"verbatus-{session_id}",
        "volume_name": f"verbatus-volume-{session_id}",
        "controller_challenge": uuid.uuid4().hex + uuid.uuid4().hex,
        "paid_actions": "disabled-until-exact-session-authorization",
    }
    durable_write(session_dir / "identity.json", identity, exclusive=True)
    durable_write(
        session_dir / "lifecycle.json",
        {
            "phase": "prepared",
            "volume_create_attempted": False,
            "volume_create_outcome": "not-attempted",
            "volume_id": None,
            "pod_create_attempted": False,
            "pod_create_outcome": "not-attempted",
            "pod_id": None,
            "pod_ever_observed": False,
            "runtime_acknowledged": False,
            "close_requested_at": None,
            "volume_delete_requested_at": None,
            "close_verification": None,
        },
        exclusive=True,
    )
    event(session_dir, "session-prepared", session_id=session_id)
    return session_dir


def session_identity(session_dir: Path) -> dict[str, Any]:
    value = read_json(session_dir / "identity.json", "session identity")
    if value.get("schema") != SCHEMA_SESSION or value.get("session_id") != session_dir.name:
        raise Refusal("session identity does not bind to its directory")
    return value


def lifecycle(session_dir: Path) -> dict[str, Any]:
    return read_json(session_dir / "lifecycle.json", "lifecycle")


def write_lifecycle(session_dir: Path, value: Mapping[str, object]) -> None:
    durable_write(session_dir / "lifecycle.json", dict(value))


AUTHORIZATION_FIELDS = {
    "schema",
    "authorize_paid_actions",
    "session_id",
    "action",
    "authorized_at",
    "expires_at",
    "acknowledges_combined_hourly_above_usd_1",
    "image",
    "image_config_digest",
    "gpu_id",
    "gpu_count",
    "cloud",
    "data_center_id",
    "quoted_gpu_hourly_usd",
    "pod_disk_gb",
    "quoted_pod_disk_hourly_usd",
    "expected_pod_hourly_usd",
    "max_pod_hourly_usd",
    "volume_size_gb",
    "volume_type",
    "expected_volume_hourly_usd",
    "max_volume_hourly_usd",
    "max_combined_hourly_usd",
    "planning_cap_seconds",
    "retrieval_margin_seconds",
    "cleanup_margin_seconds",
    "billing_cutoff_margin_seconds",
    "volume_disposition",
    "merged_repository_commit",
}


def validate_authorization(
    authorization: Mapping[str, object],
    identity: Mapping[str, object],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    if set(authorization) != AUTHORIZATION_FIELDS:
        missing = sorted(AUTHORIZATION_FIELDS - set(authorization))
        extra = sorted(set(authorization) - AUTHORIZATION_FIELDS)
        raise Refusal(f"authorization has wrong closed shape; missing={missing}, extra={extra}")
    if authorization["schema"] != SCHEMA_AUTHORIZATION:
        raise Refusal("authorization schema is not supported")
    if authorization["authorize_paid_actions"] is not True:
        raise Refusal("authorization does not explicitly authorize paid actions")
    if authorization["session_id"] != identity["session_id"]:
        raise Refusal("authorization is not for this exact random session")
    if authorization["action"] != "create-one-temporary-volume-and-one-gpu-pod":
        raise Refusal("authorization action is not the one supported action")
    authorized_at = parse_time(authorization["authorized_at"], "authorized_at")
    expires_at = parse_time(authorization["expires_at"], "expires_at")
    if authorized_at > now + dt.timedelta(seconds=30) or not authorized_at <= now < expires_at:
        raise Refusal("authorization is not currently valid")
    if expires_at - authorized_at > dt.timedelta(hours=1):
        raise Refusal("authorization validity window exceeds one hour")
    fixed = {
        "image": PINNED_IMAGE,
        "image_config_digest": PINNED_IMAGE_CONFIG,
        "gpu_count": 1,
        "cloud": "SECURE",
        "volume_disposition": "delete-after-evidence-retrieval-or-at-cleanup-deadline",
    }
    for key, expected in fixed.items():
        if authorization[key] != expected:
            raise Refusal(f"authorization {key} must equal {expected!r}")
    if not isinstance(authorization["gpu_id"], str) or not authorization["gpu_id"]:
        raise Refusal("gpu_id must be non-empty")
    if not isinstance(authorization["data_center_id"], str) or not authorization["data_center_id"]:
        raise Refusal("data_center_id must be non-empty")
    if authorization["volume_type"] not in ("STANDARD", "HIGH_PERFORMANCE"):
        raise Refusal("volume_type must be STANDARD or HIGH_PERFORMANCE")
    integers = {
        "pod_disk_gb": (1, 4096),
        "volume_size_gb": (10, 4096),
        "planning_cap_seconds": (1, MAX_TOTAL_SECONDS),
        "retrieval_margin_seconds": (60, 3600),
        "cleanup_margin_seconds": (60, 1800),
        "billing_cutoff_margin_seconds": (0, 3600),
    }
    for key, (minimum, maximum) in integers.items():
        value = authorization[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise Refusal(f"authorization {key} must be an integer in [{minimum}, {maximum}]")
    if (
        authorization["retrieval_margin_seconds"]
        + authorization["cleanup_margin_seconds"]
        >= authorization["planning_cap_seconds"]
    ):
        raise Refusal("retrieval and cleanup margins consume the whole planning cap")
    commit = authorization["merged_repository_commit"]
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise Refusal("merged_repository_commit must be a full lowercase SHA-1")
    money = {
        key: decimal(authorization[key], key)
        for key in (
            "quoted_gpu_hourly_usd",
            "quoted_pod_disk_hourly_usd",
            "expected_pod_hourly_usd",
            "max_pod_hourly_usd",
            "expected_volume_hourly_usd",
            "max_volume_hourly_usd",
            "max_combined_hourly_usd",
        )
    }
    if money["expected_pod_hourly_usd"] != (
        money["quoted_gpu_hourly_usd"] + money["quoted_pod_disk_hourly_usd"]
    ):
        raise Refusal("expected pod rate must equal the quoted GPU plus pod-disk rates")
    if money["expected_pod_hourly_usd"] > money["max_pod_hourly_usd"]:
        raise Refusal("expected pod rate exceeds its authorization ceiling")
    if money["expected_volume_hourly_usd"] > money["max_volume_hourly_usd"]:
        raise Refusal("expected volume rate exceeds its authorization ceiling")
    combined = money["expected_pod_hourly_usd"] + money["expected_volume_hourly_usd"]
    if combined > money["max_combined_hourly_usd"]:
        raise Refusal("expected combined rate exceeds its authorization ceiling")
    if combined > Decimal("1") and authorization["acknowledges_combined_hourly_above_usd_1"] is not True:
        raise Refusal("combined rate is above USD 1/hour without explicit acknowledgement")
    normalized = dict(authorization)
    normalized["authorization_sha256"] = sha256_bytes(canonical_bytes(dict(authorization)))
    return normalized


def authorization_preview(authorization: Mapping[str, object], identity: Mapping[str, object]) -> dict[str, Any]:
    checked = validate_authorization(authorization, identity)
    hours = Decimal(checked["planning_cap_seconds"]) / Decimal(3600)
    combined = decimal(checked["expected_pod_hourly_usd"], "pod rate") + decimal(
        checked["expected_volume_hourly_usd"], "volume rate"
    )
    return {
        "session_id": identity["session_id"],
        "pod_name": identity["pod_name"],
        "volume_name": identity["volume_name"],
        "image": checked["image"],
        "gpu": {"id": checked["gpu_id"], "count": checked["gpu_count"]},
        "cloud": checked["cloud"],
        "data_center_id": checked["data_center_id"],
        "pod_disk_gb": checked["pod_disk_gb"],
        "network_volume": {
            "size_gb": checked["volume_size_gb"],
            "type": checked["volume_type"],
            "disposition": checked["volume_disposition"],
        },
        "combined_hourly_usd": str(combined),
        "planning_cap_seconds": checked["planning_cap_seconds"],
        "maximum_planned_cost_usd": str(combined * hours),
        "paid_actions": "still disabled; preview performs no provider call",
        "authorization_sha256": checked["authorization_sha256"],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class UrllibTransport:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise Refusal("RunPod API key is empty")
        self._key = api_key
        self._opener = urllib.request.build_opener(NoRedirect)

    def __call__(
        self, method: str, route: str, body: Mapping[str, object] | None
    ) -> tuple[int, object | None]:
        data = canonical_bytes(body) if body is not None else None
        request = urllib.request.Request(
            urllib.parse.urljoin(API_ROOT, route),
            data=data,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                if response.geturl() != request.full_url:
                    raise TransportUncertain("redirect refused")
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise TransportUncertain("provider response exceeded two megabytes")
                parsed = json.loads(raw) if raw else None
                return response.status, parsed
        except urllib.error.HTTPError as error:
            # Deliberately discard the body. Some providers echo rejected env values.
            with contextlib.suppress(Exception):
                error.close()
            return error.code, None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise TransportUncertain(type(error).__name__) from error


def load_api_key() -> str:
    path = Path.home() / ".runpod/config.toml"
    try:
        value = tomllib.loads(path.read_text()).get("apikey")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Refusal(f"cannot read RunPod credentials: {type(error).__name__}") from error
    if not isinstance(value, str) or not value.strip():
        raise Refusal("RunPod credentials do not contain a non-empty apikey")
    return value.strip()


class RunPodV2:
    def __init__(self, transport: Callable[[str, str, Mapping[str, object] | None], tuple[int, object | None]]) -> None:
        self._transport = transport

    def request(
        self,
        method: str,
        route: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, object | None]:
        return self._transport(method, route, body)

    def list_pods(self) -> list[dict[str, Any]]:
        pods: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            query: dict[str, object] = {"includeClusterPods": "true", "limit": 1000}
            if cursor is not None:
                query["cursor"] = cursor
            route = "pods?" + urllib.parse.urlencode(query)
            status, value = self.request("GET", route)
            if status != 200 or not isinstance(value, dict) or not isinstance(value.get("pods"), list):
                raise Refusal(f"pod inventory was not a valid HTTP 200 v2 envelope (status {status})")
            page = value.get("pagination")
            if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
                raise Refusal("pod inventory omitted its pagination contract")
            for pod in value["pods"]:
                if not isinstance(pod, dict):
                    raise Refusal("pod inventory contains a non-object row")
                pods.append(pod)
            if not page["hasNextPage"]:
                if page.get("nextCursor") is not None:
                    raise Refusal("final pod page carried a next cursor")
                return pods
            next_cursor = page.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise Refusal("pod inventory pagination did not advance")
            seen.add(next_cursor)
            cursor = next_cursor
        raise Refusal("pod inventory exceeded the bounded page count")

    def list_volumes(self) -> list[dict[str, Any]]:
        status, value = self.request("GET", "network-volumes")
        if status != 200 or not isinstance(value, dict) or not isinstance(
            value.get("networkVolumes"), list
        ):
            raise Refusal(f"volume inventory was not a valid HTTP 200 v2 envelope (status {status})")
        if not all(isinstance(row, dict) for row in value["networkVolumes"]):
            raise Refusal("volume inventory contains a non-object row")
        return list(value["networkVolumes"])

    def get_pod(self, pod_id: str) -> tuple[int, dict[str, Any] | None]:
        status, value = self.request("GET", "pods/" + urllib.parse.quote(pod_id, safe=""))
        if status == 404:
            return status, None
        if status != 200 or not isinstance(value, dict):
            raise Refusal(f"exact pod GET was not a valid HTTP 200/404 response (status {status})")
        return status, value

    def get_volume(self, volume_id: str) -> tuple[int, dict[str, Any] | None]:
        status, value = self.request(
            "GET", "network-volumes/" + urllib.parse.quote(volume_id, safe="")
        )
        if status == 404:
            return status, None
        if status != 200 or not isinstance(value, dict):
            raise Refusal(f"exact volume GET was not a valid HTTP 200/404 response (status {status})")
        return status, value

    def delete_pod(self, pod_id: str) -> int:
        status, _ = self.request("DELETE", "pods/" + urllib.parse.quote(pod_id, safe=""))
        return status

    def delete_volume(self, volume_id: str) -> int:
        status, _ = self.request(
            "DELETE", "network-volumes/" + urllib.parse.quote(volume_id, safe="")
        )
        return status


def expected_volume(identity: Mapping[str, object], authorization: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": identity["volume_name"],
        "size": authorization["volume_size_gb"],
        "dataCenter": authorization["data_center_id"],
        "type": authorization["volume_type"],
    }


def validate_volume(
    volume: Mapping[str, object], identity: Mapping[str, object], authorization: Mapping[str, object]
) -> str:
    wanted = expected_volume(identity, authorization)
    for key, expected in wanted.items():
        if volume.get(key) != expected:
            raise Refusal(f"volume response {key} did not equal the exact authorized value")
    volume_id = volume.get("id")
    if not isinstance(volume_id, str) or not volume_id:
        raise Refusal("volume response omitted a non-empty id")
    return volume_id


POD_DEADMAN_SOURCE = r'''
import ctypes,datetime as dt,hashlib,json,os,pwd,signal,subprocess,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
os.umask(0o077)
session=os.environ['VERBATUS_SESSION_ID']; challenge=os.environ['VERBATUS_CONTROLLER_CHALLENGE']
deadline=float(os.environ['VERBATUS_HARD_DEADLINE_EPOCH']); cleanup=float(os.environ['VERBATUS_CLEANUP_EPOCH'])
api_key=os.environ.pop('VERBATUS_RUNPOD_API_KEY'); pod_id=os.environ['RUNPOD_POD_ID']
root=Path('/workspace/private/session-evidence')/session; root.mkdir(parents=True,exist_ok=True); os.chmod(root,0o700)
runtime=Path('/run/verbatus-live')/session; runtime.mkdir(parents=True,exist_ok=True); os.chmod(runtime,0o711)
def write(path,obj,mode=0o600):
 data=(json.dumps(obj,sort_keys=True,separators=(',',':'))+'\n').encode(); tmp=path.with_name('.'+path.name+'.tmp')
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
 try: os.write(fd,data); os.fsync(fd)
 finally: os.close(fd)
 os.replace(tmp,path); os.chmod(path,mode); d=os.open(path.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)); os.fsync(d); os.close(d)
 return data
def refuse(reason):
 write(root/'runtime-refusal.json',{'schema':'verbatus-pod-runtime-refusal.v1','session_id':session,'pod_id':pod_id,'reason':reason,'at':time.time()})
 terminate('boot-refusal:'+reason)
def demote(uid,gid):
 def child():
  os.setgroups([]); os.setgid(gid); os.setuid(uid)
  if ctypes.CDLL(None).prctl(38,1,0,0,0)!=0: os._exit(126)
 return child
def terminate(reason):
 write(root/'deadman-terminating.json',{'schema':'verbatus-pod-deadman-termination.v1','session_id':session,'pod_id':pod_id,'reason':reason,'requested_cutoff':time.time()})
 opener=urllib.request.build_opener(type('NoRedirect',(urllib.request.HTTPRedirectHandler,),{'redirect_request':lambda self,*a,**k:None})())
 for attempt in range(1,5):
  result={'attempt':attempt,'at':time.time(),'http':None,'error':None}
  try:
   req=urllib.request.Request('https://api.runpod.io/v2/pods/'+urllib.parse.quote(pod_id,safe=''),headers={'Authorization':'Bearer '+api_key,'User-Agent':'verbatus-readiness/1.0'},method='DELETE')
   with opener.open(req,timeout=15) as response: result['http']=response.status
  except urllib.error.HTTPError as error: result['http']=error.code
  except Exception as error: result['error']=type(error).__name__
  write(root/f'deadman-attempt-{attempt}.json',result)
  if result['http'] in (204,404): break
  time.sleep(10)
 time.sleep(30); raise SystemExit(3)
if os.geteuid()!=0: refuse('deadman-not-root')
if os.getpid()!=1: refuse('deadman-not-pid1')
if deadline-time.time()>7200 or cleanup>=deadline or cleanup<=time.time(): refuse('invalid-deadline')
try: worker=pwd.getpwnam('verbatus-worker')
except KeyError:
 result=subprocess.run(['/usr/sbin/useradd','--system','--create-home','--shell','/bin/bash','verbatus-worker'],env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin'},capture_output=True)
 if result.returncode: refuse('worker-user-creation-failed')
 worker=pwd.getpwnam('verbatus-worker')
launcher="""#!/usr/bin/python3
import ctypes,json,os,sys
from pathlib import Path
session=os.environ.get("VERBATUS_SESSION_ID","")
marker=Path("/run/verbatus-live")/session/"inference-enabled.json"
if os.geteuid()==0 or not marker.is_file(): raise SystemExit("worker gate refused")
status=Path("/proc/self/status").read_text()
if "CapEff:\\t0000000000000000" not in status: raise SystemExit("worker capabilities are not empty")
if ctypes.CDLL(None).prctl(38,1,0,0,0)!=0: raise SystemExit("cannot set no-new-privileges")
for key in tuple(os.environ):
 if any(marker in key for marker in ("API_KEY","TOKEN","SECRET","CONTROLLER_CHALLENGE")): os.environ.pop(key,None)
os.execvpe(sys.argv[1],sys.argv[1:],os.environ)
"""
launcher_path=Path('/usr/local/bin/verbatus-worker-exec'); launcher_path.write_text(launcher); os.chown(launcher_path,0,0); os.chmod(launcher_path,0o755)
probe=r"""import json,os,shutil,subprocess
result={'uid':os.geteuid(),'gid':os.getegid(),'provider_env_absent':all(not any(marker in k for marker in ('API_KEY','TOKEN','SECRET','CONTROLLER_CHALLENGE')) for k in os.environ),'proc1_environ_denied':False,'root_receipt_denied':False,'sudo_unavailable':False,'cap_eff_zero':False,'no_new_privs':False}
try: open('/proc/1/environ','rb').read(1)
except OSError: result['proc1_environ_denied']=True
try: os.listdir(os.environ['VERBATUS_ROOT_RECEIPT_DIR'])
except OSError: result['root_receipt_denied']=True
sudo=shutil.which('sudo'); result['sudo_unavailable']=sudo is None or subprocess.run([sudo,'-n','true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
for line in open('/proc/self/status'):
 if line.startswith('CapEff:'): result['cap_eff_zero']=int(line.split()[1],16)==0
 if line.startswith('NoNewPrivs:'): result['no_new_privs']=line.split()[1]=='1'
print(json.dumps(result,sort_keys=True))"""
child_env={k:v for k,v in os.environ.items() if not any(marker in k for marker in ('API_KEY','TOKEN','SECRET','CONTROLLER_CHALLENGE'))}; child_env['VERBATUS_ROOT_RECEIPT_DIR']=str(root)
probe_run=subprocess.run(['python3','-c',probe],env=child_env,capture_output=True,text=True,preexec_fn=demote(worker.pw_uid,worker.pw_gid),timeout=20)
try: probe_result=json.loads(probe_run.stdout)
except Exception: refuse('worker-probe-unreadable')
required=('provider_env_absent','proc1_environ_denied','root_receipt_denied','sudo_unavailable','cap_eff_zero','no_new_privs')
if probe_run.returncode or probe_result.get('uid')==0 or not all(probe_result.get(k) is True for k in required): refuse('worker-separation-unverified')
service_env={k:v for k,v in os.environ.items() if k!='VERBATUS_ROOT_RECEIPT_DIR' and not any(marker in k for marker in ('API_KEY','TOKEN','SECRET','CONTROLLER_CHALLENGE'))}
service=subprocess.Popen(['/start.sh'],env=service_env)
time.sleep(5)
if service.poll() is not None: refuse('default-start-service-exited')
receipt={'schema':'verbatus-pod-runtime-receipt.v1','session_id':session,'pod_id':pod_id,'controller_challenge':challenge,'hard_deadline_epoch':deadline,'cleanup_epoch':cleanup,'pid':os.getpid(),'uid':os.geteuid(),'default_start_pid':service.pid,'worker_uid':worker.pw_uid,'worker_gid':worker.pw_gid,'worker_probe':probe_result,'provider_key_removed_before_start':True,'runtime_verified':True,'observed_at':time.time()}
receipt_bytes=write(root/'runtime-receipt.json',receipt)
receipt_sha=hashlib.sha256(receipt_bytes).hexdigest(); ack_path=root/'controller-ack.json'; enabled=False
while time.time()<cleanup:
 if (root/'STOP').exists(): terminate('durable-stop-flag')
 if service.poll() is not None: terminate('default-start-service-exited')
 if not enabled and ack_path.exists():
  try: ack=json.loads(ack_path.read_text())
  except Exception: terminate('controller-ack-unreadable')
  expected={'schema':'verbatus-controller-ack.v1','session_id':session,'pod_id':pod_id,'controller_challenge':challenge,'hard_deadline_epoch':deadline,'runtime_receipt_sha256':receipt_sha}
  if any(ack.get(k)!=v for k,v in expected.items()): terminate('controller-ack-mismatch')
  write(runtime/'inference-enabled.json',{'session_id':session,'pod_id':pod_id,'runtime_receipt_sha256':receipt_sha},0o644)
  write(root/'controller-acknowledged.json',{'schema':'verbatus-pod-controller-acknowledgement.v1','session_id':session,'pod_id':pod_id,'acknowledged_at':time.time(),'runtime_receipt_sha256':receipt_sha})
  enabled=True
 time.sleep(min(5,max(0,cleanup-time.time())))
terminate('cleanup-deadline')
'''


def pod_command() -> tuple[str, list[str], list[str]]:
    cmd = ["python3", "-u", "-c", POD_DEADMAN_SOURCE]
    args = json.dumps({"cmd": cmd}, separators=(",", ":"))
    return args, PINNED_ENTRYPOINT, cmd


def pod_environment(
    identity: Mapping[str, object], deadlines: Mapping[str, object], api_key: str
) -> dict[str, str]:
    return {
        "VERBATUS_SESSION_ID": str(identity["session_id"]),
        "VERBATUS_CONTROLLER_CHALLENGE": str(identity["controller_challenge"]),
        "VERBATUS_HARD_DEADLINE_EPOCH": str(deadlines["hard_deadline_epoch"]),
        "VERBATUS_CLEANUP_EPOCH": str(deadlines["cleanup_epoch"]),
        "VERBATUS_RUNPOD_API_KEY": api_key,
    }


def pod_request(
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
    volume_id: str,
    deadlines: Mapping[str, object],
    api_key: str,
) -> dict[str, object]:
    args, _, _ = pod_command()
    return {
        "name": identity["pod_name"],
        "cloud": authorization["cloud"],
        "image": authorization["image"],
        "gpu": {"id": authorization["gpu_id"], "count": authorization["gpu_count"]},
        "dataCenterIds": [authorization["data_center_id"]],
        "disk": authorization["pod_disk_gb"],
        "mounts": {"network": [{"volumeId": volume_id, "path": "/workspace/private"}]},
        "ports": ["22/tcp"],
        "startSsh": True,
        "startJupyter": False,
        "args": args,
        "env": pod_environment(identity, deadlines, api_key),
    }


def redacted_pod_request(request: Mapping[str, object]) -> dict[str, object]:
    result = dict(request)
    env = result.get("env")
    if isinstance(env, dict):
        result["env"] = {key: "[supplied-privately]" for key in env}
    return result


def validate_pod(
    pod: Mapping[str, object],
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
    volume_id: str,
    deadlines: Mapping[str, object],
    api_key: str,
) -> str:
    args, entrypoint, cmd = pod_command()
    expected = {
        "name": identity["pod_name"],
        "cloud": authorization["cloud"],
        "image": authorization["image"],
        "disk": authorization["pod_disk_gb"],
        "args": args,
        "entrypoint": entrypoint,
        "cmd": cmd,
        "dataCenterId": authorization["data_center_id"],
        "mounts": {"network": [{"volumeId": volume_id, "path": "/workspace/private"}]},
    }
    for key, wanted in expected.items():
        if pod.get(key) != wanted:
            raise Refusal(f"pod response {key} did not equal the exact authorized value")
    gpu = pod.get("gpu")
    if not isinstance(gpu, dict) or gpu.get("id") != authorization["gpu_id"] or gpu.get(
        "count"
    ) != authorization["gpu_count"]:
        raise Refusal("pod response GPU id/count did not equal the exact authorized value")
    if decimal(pod.get("cost"), "pod response cost") != decimal(
        authorization["expected_pod_hourly_usd"], "expected pod rate"
    ):
        raise Refusal("pod response price did not equal the exact authorized quoted price")
    if decimal(pod.get("cost"), "pod response cost") > decimal(
        authorization["max_pod_hourly_usd"], "maximum pod rate"
    ):
        raise Refusal("pod response price exceeded the authorized ceiling")
    status = pod.get("status")
    if status not in {"PROVISIONING", "STARTING", "RUNNING"}:
        raise Refusal(f"pod response status {status!r} is not a live startup status")
    returned_env = pod.get("env")
    expected_env = pod_environment(identity, deadlines, api_key)
    if not isinstance(returned_env, dict) or any(returned_env.get(k) != v for k, v in expected_env.items()):
        raise Refusal("pod response did not retain the exact controller environment")
    pod_id = pod.get("id")
    if not isinstance(pod_id, str) or not pod_id:
        raise Refusal("pod response omitted a non-empty id")
    return pod_id


def exact_named(rows: list[dict[str, Any]], name: object) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("name") == name]


def record_deadlines(life: dict[str, Any], authorization: Mapping[str, object], now: dt.datetime) -> None:
    hard = now + dt.timedelta(seconds=int(authorization["planning_cap_seconds"]))
    cleanup = hard - dt.timedelta(seconds=int(authorization["cleanup_margin_seconds"]))
    inference = cleanup - dt.timedelta(seconds=int(authorization["retrieval_margin_seconds"]))
    life.update(
        {
            "create_window_started_at": iso(now),
            "hard_deadline": iso(hard),
            "hard_deadline_epoch": hard.timestamp(),
            "cleanup_at": iso(cleanup),
            "cleanup_epoch": cleanup.timestamp(),
            "inference_cutoff": iso(inference),
            "billing_reconcile_until": iso(hard + dt.timedelta(seconds=POST_DELETE_BILLING_WAIT_SECONDS)),
        }
    )


def verify_watchdog_prearmed(session_dir: Path, now: dt.datetime | None = None) -> None:
    now = now or utc_now()
    ready = read_json(session_dir / "watchdog-ready.json", "watchdog readiness")
    if ready.get("session_id") != session_dir.name or not isinstance(ready.get("pid"), int):
        raise Refusal("watchdog readiness is not bound to this session")
    observed = parse_time(ready.get("heartbeat_at"), "watchdog heartbeat")
    if not 0 <= (now - observed).total_seconds() <= WATCHDOG_READY_MAX_AGE_SECONDS:
        raise Refusal("watchdog heartbeat is stale or future-dated")
    try:
        os.kill(ready["pid"], 0)
    except OSError as error:
        raise Refusal("watchdog process is not alive") from error
    try:
        with exclusive_lock(session_dir / "watchdog.lock", blocking=False):
            raise Refusal("watchdog lock is not held")
    except BlockingIOError:
        pass


def write_public_launch_evidence(
    session_dir: Path,
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
    request: Mapping[str, object],
) -> None:
    durable_write(
        session_dir / "launch-plan-redacted.json",
        {
            "session_id": identity["session_id"],
            "authorization_sha256": authorization["authorization_sha256"],
            "request": redacted_pod_request(request),
            "credential_policy": "provider key values excluded",
        },
    )


def reconcile_volume(
    api: RunPodV2,
    session_dir: Path,
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
) -> str | None:
    life = lifecycle(session_dir)
    matches = exact_named(api.list_volumes(), identity["volume_name"])
    if len(matches) == 1:
        volume_id = validate_volume(matches[0], identity, authorization)
        _, exact = api.get_volume(volume_id)
        if exact is None:
            raise Refusal("named volume vanished before exact GET validation")
        validate_volume(exact, identity, authorization)
        life["volume_id"] = volume_id
        life["volume_create_outcome"] = "confirmed"
        life["phase"] = "volume-bound"
        write_lifecycle(session_dir, life)
        event(session_dir, "volume-adopted-after-reconciliation", volume_id=volume_id)
        return volume_id
    if not matches:
        event(session_dir, "volume-create-outcome-still-uncertain", exact_name_matches=0)
        return None
    event(session_dir, "duplicate-exact-volume-matches", count=len(matches))
    for row in matches:
        volume_id = row.get("id")
        if isinstance(volume_id, str) and volume_id:
            status = api.delete_volume(volume_id)
            event(session_dir, "duplicate-volume-delete-requested", volume_id=volume_id, http=status)
    life["phase"] = "close-unverified"
    write_lifecycle(session_dir, life)
    raise Refusal("multiple exact-name volumes existed; deletion was requested for every identifiable match")


def create_volume_once(
    api: RunPodV2,
    session_dir: Path,
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
) -> str | None:
    life = lifecycle(session_dir)
    if life.get("volume_id"):
        return str(life["volume_id"])
    if life.get("volume_create_attempted"):
        return reconcile_volume(api, session_dir, identity, authorization)
    life["volume_create_attempted"] = True
    life["volume_create_outcome"] = "unknown"
    life["phase"] = "volume-create-pending"
    write_lifecycle(session_dir, life)
    body = expected_volume(identity, authorization)
    event(session_dir, "volume-create-intent-recorded", volume_name=identity["volume_name"])
    try:
        status, value = api.request("POST", "network-volumes", body)
    except TransportUncertain:
        event(session_dir, "volume-create-response-uncertain")
        return reconcile_volume(api, session_dir, identity, authorization)
    if status != 201 or not isinstance(value, dict):
        event(session_dir, "volume-create-not-confirmed", http=status)
        if 400 <= status < 500:
            life = lifecycle(session_dir)
            life["volume_create_outcome"] = "rejected"
            life["phase"] = "volume-create-rejected"
            write_lifecycle(session_dir, life)
            return None
        return reconcile_volume(api, session_dir, identity, authorization)
    try:
        volume_id = validate_volume(value, identity, authorization)
    except Refusal:
        possible_id = value.get("id")
        if isinstance(possible_id, str) and possible_id:
            life = lifecycle(session_dir)
            life["volume_id"] = possible_id
            life["volume_create_outcome"] = "response-invalid"
            life["phase"] = "invalid-volume-closing"
            write_lifecycle(session_dir, life)
            delete_status = api.delete_volume(possible_id)
            event(session_dir, "invalid-volume-response-delete-requested", volume_id=possible_id, http=delete_status)
        raise
    life = lifecycle(session_dir)
    life["volume_id"] = volume_id
    life["volume_create_outcome"] = "confirmed"
    life["volume_created_at"] = iso(utc_now())
    life["phase"] = "volume-bound"
    write_lifecycle(session_dir, life)
    event(session_dir, "volume-created", volume_id=volume_id)
    return volume_id


def reconcile_pod(
    api: RunPodV2,
    session_dir: Path,
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
    deadlines: Mapping[str, object],
    api_key: str,
) -> str | None:
    life = lifecycle(session_dir)
    matches = exact_named(api.list_pods(), identity["pod_name"])
    if len(matches) == 1:
        pod_id = validate_pod(matches[0], identity, authorization, str(life["volume_id"]), deadlines, api_key)
        _, exact = api.get_pod(pod_id)
        if exact is None:
            raise Refusal("named pod vanished before exact GET validation")
        validate_pod(exact, identity, authorization, str(life["volume_id"]), deadlines, api_key)
        life["pod_id"] = pod_id
        life["pod_ever_observed"] = True
        life["pod_create_outcome"] = "confirmed"
        life["phase"] = "pod-bound-awaiting-runtime-ack"
        write_lifecycle(session_dir, life)
        event(session_dir, "pod-adopted-after-reconciliation", pod_id=pod_id)
        return pod_id
    if not matches:
        event(session_dir, "pod-create-outcome-still-uncertain", exact_name_matches=0)
        return None
    event(session_dir, "duplicate-exact-pod-matches", count=len(matches))
    for row in matches:
        pod_id = row.get("id")
        if isinstance(pod_id, str) and pod_id:
            status = api.delete_pod(pod_id)
            event(session_dir, "duplicate-pod-delete-requested", pod_id=pod_id, http=status)
    life["phase"] = "close-unverified"
    write_lifecycle(session_dir, life)
    raise Refusal("multiple exact-name pods existed; deletion was requested for every identifiable match")


def create_pod_once(
    api: RunPodV2,
    session_dir: Path,
    identity: Mapping[str, object],
    authorization: Mapping[str, object],
    deadlines: Mapping[str, object],
    api_key: str,
) -> str | None:
    life = lifecycle(session_dir)
    if life.get("pod_id"):
        return str(life["pod_id"])
    if life.get("pod_create_attempted"):
        return reconcile_pod(api, session_dir, identity, authorization, deadlines, api_key)
    volume_id = life.get("volume_id")
    if not isinstance(volume_id, str) or not volume_id:
        raise Refusal("pod create cannot begin before an exact volume is bound")
    request = pod_request(identity, authorization, volume_id, deadlines, api_key)
    write_public_launch_evidence(session_dir, identity, authorization, request)
    life["pod_create_attempted"] = True
    life["pod_create_outcome"] = "unknown"
    life["phase"] = "pod-create-pending"
    write_lifecycle(session_dir, life)
    event(session_dir, "pod-create-intent-recorded", pod_name=identity["pod_name"])
    try:
        status, value = api.request("POST", "pods", request)
    except TransportUncertain:
        event(session_dir, "pod-create-response-uncertain")
        return reconcile_pod(api, session_dir, identity, authorization, deadlines, api_key)
    if status != 201 or not isinstance(value, dict):
        event(session_dir, "pod-create-not-confirmed", http=status)
        if 400 <= status < 500:
            life = lifecycle(session_dir)
            life["pod_create_outcome"] = "rejected"
            life["phase"] = "pod-create-rejected"
            write_lifecycle(session_dir, life)
            return None
        return reconcile_pod(api, session_dir, identity, authorization, deadlines, api_key)
    try:
        pod_id = validate_pod(value, identity, authorization, volume_id, deadlines, api_key)
    except Refusal:
        possible_id = value.get("id")
        if isinstance(possible_id, str) and possible_id:
            life = lifecycle(session_dir)
            life["pod_id"] = possible_id
            life["pod_ever_observed"] = True
            life["pod_create_outcome"] = "response-invalid"
            life["phase"] = "invalid-pod-closing"
            write_lifecycle(session_dir, life)
            delete_status = api.delete_pod(possible_id)
            event(session_dir, "invalid-pod-response-delete-requested", pod_id=possible_id, http=delete_status)
        raise
    life = lifecycle(session_dir)
    life["pod_id"] = pod_id
    life["pod_ever_observed"] = True
    life["pod_create_outcome"] = "confirmed"
    life["pod_created_at"] = value.get("createdAt")
    life["phase"] = "pod-bound-awaiting-runtime-ack"
    write_lifecycle(session_dir, life)
    event(session_dir, "pod-created", pod_id=pod_id, status=value.get("status"))
    return pod_id


def verify_catalog_quote(api: RunPodV2, authorization: Mapping[str, object]) -> None:
    route = "catalog/gpus?" + urllib.parse.urlencode(
        {"include": "AVAILABILITY", "product": "POD", "cloud": authorization["cloud"]}
    )
    status, value = api.request("GET", route)
    if status != 200 or not isinstance(value, dict) or not isinstance(value.get("gpus"), list):
        raise Refusal(f"GPU catalogue was not a valid HTTP 200 envelope (status {status})")
    matches = [row for row in value["gpus"] if isinstance(row, dict) and row.get("id") == authorization["gpu_id"]]
    if len(matches) != 1:
        raise Refusal("GPU catalogue did not contain exactly one authorized GPU row")
    price = matches[0].get("price")
    if not isinstance(price, dict) or decimal(price.get("secure"), "catalogue secure price") != decimal(
        authorization["quoted_gpu_hourly_usd"], "authorized GPU quote"
    ):
        raise Refusal("current secure GPU price differs from the exact authorized quote")
    centers = matches[0].get("dataCenters")
    available = False
    if isinstance(centers, list):
        available = any(
            isinstance(row, dict)
            and row.get("id") == authorization["data_center_id"]
            and row.get("availability") not in (None, "NONE", 0)
            for row in centers
        )
    if not available:
        raise Refusal("authorized GPU/data-center combination is not advertised as available")


def launch(
    api: RunPodV2,
    session_dir: Path,
    authorization: Mapping[str, object],
    api_key: str,
) -> dict[str, object]:
    identity = session_identity(session_dir)
    checked = validate_authorization(authorization, identity)
    verify_watchdog_prearmed(session_dir)
    life = lifecycle(session_dir)
    if life.get("phase") in {"closed-verified", "close-unverified"}:
        raise Refusal("session is already in a terminal close phase")
    if not life.get("create_window_started_at"):
        record_deadlines(life, checked, utc_now())
        life["authorization_sha256"] = checked["authorization_sha256"]
        life["authorization_public"] = {
            key: value for key, value in checked.items() if key != "authorization_sha256"
        }
        write_lifecycle(session_dir, life)
    elif life.get("authorization_sha256") != checked["authorization_sha256"]:
        raise Refusal("resuming a session requires the exact original authorization bytes")
    verify_catalog_quote(api, checked)
    volume_id = create_volume_once(api, session_dir, identity, checked)
    if volume_id is None:
        return {"green": False, "state": "volume-create-uncertain", "session_id": identity["session_id"]}
    life = lifecycle(session_dir)
    deadlines = {
        "hard_deadline_epoch": life["hard_deadline_epoch"],
        "cleanup_epoch": life["cleanup_epoch"],
    }
    pod_id = create_pod_once(api, session_dir, identity, checked, deadlines, api_key)
    if pod_id is None:
        return {"green": False, "state": "pod-create-uncertain", "session_id": identity["session_id"]}
    return {
        "green": False,
        "state": "pod-bound-awaiting-runtime-receipt-and-controller-ack",
        "session_id": identity["session_id"],
        "pod_id": pod_id,
        "volume_id": volume_id,
        "inference_enabled": False,
    }


def billing_verified(
    value: object,
    *,
    id_field: str,
    resource_id: str,
    created_at: str,
    cutoff: str,
) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("records"), list) or not value["records"]:
        return False
    metadata = value.get("metadata")
    query = metadata.get("query") if isinstance(metadata, dict) else None
    if not isinstance(query, dict) or query.get(id_field) != resource_id:
        return False
    try:
        query_start = parse_time(query.get("startTime"), "billing query start")
        query_end = parse_time(query.get("endTime"), "billing query end")
        created = parse_time(created_at, "resource creation time")
        requested_cutoff = parse_time(cutoff, "requested billing cutoff")
    except Refusal:
        return False
    if query_start > created or query_end < requested_cutoff:
        return False
    record_starts: list[dt.datetime] = []
    record_ends: list[dt.datetime] = []
    for record in value["records"]:
        if not isinstance(record, dict) or record.get(id_field) != resource_id:
            return False
        try:
            record_start = parse_time(record.get("startTime"), "record start")
            record_end = parse_time(record.get("endTime"), "record end")
            if record_start < query_start:
                return False
            if record_end > query_end or record_end <= record_start:
                return False
            record_starts.append(record_start)
            record_ends.append(record_end)
        except Refusal:
            return False
    return min(record_starts) <= created and max(record_ends) >= requested_cutoff


def verify_pod_absent(api: RunPodV2, pod_id: str) -> bool:
    status, _ = api.get_pod(pod_id)
    if status != 404:
        return False
    return all(row.get("id") != pod_id for row in api.list_pods())


def verify_volume_absent(api: RunPodV2, volume_id: str) -> bool:
    status, _ = api.get_volume(volume_id)
    if status != 404:
        return False
    return all(row.get("id") != volume_id for row in api.list_volumes())


def close_resources(api: RunPodV2, session_dir: Path) -> dict[str, object]:
    life = lifecycle(session_dir)
    authorization = life.get("authorization_public")
    if not isinstance(authorization, dict):
        raise Refusal("cannot close without the session's recorded authorization")
    pod_id = life.get("pod_id")
    volume_id = life.get("volume_id")
    close_requested_at = life.get("close_requested_at") or iso(utc_now())
    life["close_requested_at"] = close_requested_at
    cutoff = life.get("billing_cutoff") or iso(
        parse_time(close_requested_at, "close request time")
        + dt.timedelta(seconds=int(authorization["billing_cutoff_margin_seconds"]))
    )
    life["billing_cutoff"] = cutoff
    write_lifecycle(session_dir, life)

    ambiguous_pod_ids: list[str] = []
    pod_create_uncertainty = bool(
        pod_id is None
        and life.get("pod_create_attempted")
        and life.get("pod_create_outcome") != "rejected"
    )
    if pod_create_uncertainty:
        try:
            matches = exact_named(api.list_pods(), session_identity(session_dir)["pod_name"])
        except (Refusal, TransportUncertain):
            matches = []
        if len(matches) == 1 and isinstance(matches[0].get("id"), str):
            pod_id = matches[0]["id"]
            life["pod_id"] = pod_id
            life["pod_ever_observed"] = True
            life["pod_created_at"] = matches[0].get("createdAt") or life.get(
                "create_window_started_at"
            )
            write_lifecycle(session_dir, life)
            pod_create_uncertainty = False
        elif len(matches) > 1:
            ambiguous_pod_ids = [
                str(row["id"])
                for row in matches
                if isinstance(row.get("id"), str) and row.get("id")
            ]
            for candidate in ambiguous_pod_ids:
                try:
                    status = api.delete_pod(candidate)
                except TransportUncertain:
                    status = 0
                event(
                    session_dir,
                    "ambiguous-pod-delete-requested",
                    pod_id=candidate,
                    http=status,
                )
    pod_absent = pod_id is None and not pod_create_uncertainty and not ambiguous_pod_ids
    pod_billing = pod_absent
    pod_delete_http: int | None = None
    if isinstance(pod_id, str) and pod_id:
        try:
            pod_delete_http = api.delete_pod(pod_id)
        except TransportUncertain:
            pod_delete_http = 0
        event(session_dir, "pod-delete-requested", pod_id=pod_id, http=pod_delete_http)
        try:
            pod_absent = verify_pod_absent(api, pod_id)
        except (Refusal, TransportUncertain):
            pod_absent = False
        if pod_absent:
            route = "billing/pods?" + urllib.parse.urlencode(
                {
                    "podId": pod_id,
                    "startTime": life.get("pod_created_at") or life["create_window_started_at"],
                    "endTime": cutoff,
                    "bucketSize": "hour",
                }
            )
            try:
                status, value = api.request("GET", route)
                pod_billing = status == 200 and billing_verified(
                    value,
                    id_field="podId",
                    resource_id=pod_id,
                    created_at=str(
                        life.get("pod_created_at") or life["create_window_started_at"]
                    ),
                    cutoff=str(cutoff),
                )
            except (Refusal, TransportUncertain):
                pod_billing = False

    ambiguous_volume_ids: list[str] = []
    volume_create_uncertainty = bool(
        volume_id is None
        and life.get("volume_create_attempted")
        and life.get("volume_create_outcome") != "rejected"
    )
    if pod_absent and volume_create_uncertainty:
        try:
            matches = exact_named(api.list_volumes(), session_identity(session_dir)["volume_name"])
        except (Refusal, TransportUncertain):
            matches = []
        if len(matches) == 1 and isinstance(matches[0].get("id"), str):
            volume_id = matches[0]["id"]
            life = lifecycle(session_dir)
            life["volume_id"] = volume_id
            life["volume_created_at"] = life.get("volume_created_at") or life.get(
                "create_window_started_at"
            )
            write_lifecycle(session_dir, life)
            volume_create_uncertainty = False
        elif len(matches) > 1:
            ambiguous_volume_ids = [
                str(row["id"])
                for row in matches
                if isinstance(row.get("id"), str) and row.get("id")
            ]
            for candidate in ambiguous_volume_ids:
                try:
                    status = api.delete_volume(candidate)
                except TransportUncertain:
                    status = 0
                event(
                    session_dir,
                    "ambiguous-volume-delete-requested",
                    volume_id=candidate,
                    http=status,
                )
    volume_absent = volume_id is None and not volume_create_uncertainty and not ambiguous_volume_ids
    volume_billing = volume_absent
    volume_delete_http: int | None = None
    if pod_absent and isinstance(volume_id, str) and volume_id:
        life = lifecycle(session_dir)
        volume_delete_requested_at = life.get("volume_delete_requested_at") or iso(utc_now())
        life["volume_delete_requested_at"] = volume_delete_requested_at
        volume_cutoff = life.get("volume_billing_cutoff") or iso(
            parse_time(volume_delete_requested_at, "volume delete request time")
            + dt.timedelta(seconds=int(authorization["billing_cutoff_margin_seconds"]))
        )
        life["volume_billing_cutoff"] = volume_cutoff
        write_lifecycle(session_dir, life)
        try:
            volume_delete_http = api.delete_volume(volume_id)
        except TransportUncertain:
            volume_delete_http = 0
        event(session_dir, "volume-delete-requested", volume_id=volume_id, http=volume_delete_http)
        try:
            volume_absent = verify_volume_absent(api, volume_id)
        except (Refusal, TransportUncertain):
            volume_absent = False
        if volume_absent:
            route = "billing/network-volumes?" + urllib.parse.urlencode(
                {
                    "networkVolumeId": volume_id,
                    "startTime": life.get("volume_created_at") or life["create_window_started_at"],
                    "endTime": volume_cutoff,
                    "bucketSize": "hour",
                }
            )
            try:
                status, value = api.request("GET", route)
                volume_billing = status == 200 and billing_verified(
                    value,
                    id_field="networkVolumeId",
                    resource_id=volume_id,
                    created_at=str(
                        life.get("volume_created_at") or life["create_window_started_at"]
                    ),
                    cutoff=str(volume_cutoff),
                )
            except (Refusal, TransportUncertain):
                volume_billing = False
    green = pod_absent and pod_billing and volume_absent and volume_billing
    result = {
        "schema": "verbatus-runpod-close-verification.v1",
        "session_id": session_dir.name,
        "pod_id": pod_id,
        "volume_id": volume_id,
        "pod_delete_http": pod_delete_http,
        "pod_create_outcome_uncertain": pod_create_uncertainty,
        "ambiguous_pod_ids_closed_but_unverified": ambiguous_pod_ids,
        "pod_get_404_and_full_list_absent": pod_absent,
        "pod_billing_nonempty_exact_through_cutoff": pod_billing,
        "volume_delete_http": volume_delete_http,
        "volume_create_outcome_uncertain": volume_create_uncertainty,
        "ambiguous_volume_ids_closed_but_unverified": ambiguous_volume_ids,
        "volume_get_404_and_list_absent": volume_absent,
        "volume_billing_nonempty_exact_through_cutoff": volume_billing,
        "requested_cutoff": cutoff,
        "green": green,
        "billing_lag": not (pod_billing and volume_billing),
        "status": "closed-verified" if green else "close-unverified",
        "observed_at": iso(utc_now()),
    }
    durable_write(session_dir / "close-verification.json", result)
    life = lifecycle(session_dir)
    life["close_verification"] = result
    life["phase"] = result["status"]
    write_lifecycle(session_dir, life)
    event(session_dir, result["status"], pod_absent=pod_absent, volume_absent=volume_absent, billing_verified=pod_billing and volume_billing)
    return result


def record_runtime_ack(session_dir: Path, receipt_path: Path, output_path: Path) -> dict[str, object]:
    identity = session_identity(session_dir)
    life = lifecycle(session_dir)
    receipt_bytes = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError as error:
        raise Refusal("runtime receipt is not valid JSON") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA_RUNTIME_RECEIPT:
        raise Refusal("runtime receipt schema is not supported")
    expected = {
        "session_id": identity["session_id"],
        "pod_id": life.get("pod_id"),
        "controller_challenge": identity["controller_challenge"],
        "hard_deadline_epoch": life.get("hard_deadline_epoch"),
        "cleanup_epoch": life.get("cleanup_epoch"),
        "pid": 1,
        "uid": 0,
        "runtime_verified": True,
        "provider_key_removed_before_start": True,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise Refusal("runtime receipt does not prove this exact session, pod, deadline, and root PID1")
    probe = receipt.get("worker_probe")
    required_probe = {
        "provider_env_absent": True,
        "proc1_environ_denied": True,
        "root_receipt_denied": True,
        "sudo_unavailable": True,
        "cap_eff_zero": True,
        "no_new_privs": True,
    }
    if not isinstance(probe, dict) or any(probe.get(k) != v for k, v in required_probe.items()):
        raise Refusal("runtime receipt does not prove non-root worker separation")
    ack = {
        "schema": SCHEMA_CONTROLLER_ACK,
        "session_id": identity["session_id"],
        "pod_id": life["pod_id"],
        "controller_challenge": identity["controller_challenge"],
        "hard_deadline_epoch": life["hard_deadline_epoch"],
        "runtime_receipt_sha256": sha256_bytes(receipt_bytes),
        "acknowledged_at": iso(utc_now()),
        "instruction": "upload as controller-ack.json beside the pod runtime receipt",
    }
    durable_write(output_path, ack, exclusive=not output_path.exists())
    life["runtime_acknowledged"] = True
    life["runtime_receipt_sha256"] = ack["runtime_receipt_sha256"]
    life["phase"] = "runtime-ack-prepared-awaiting-pod-acknowledgement"
    write_lifecycle(session_dir, life)
    event(session_dir, "runtime-receipt-accepted", pod_id=life["pod_id"])
    return ack


def close_until_bounded(api: RunPodV2, session_dir: Path, *, poll_seconds: float) -> int:
    """Keep trying to stop metered resources; only billing lag gets a bounded exit."""

    while True:
        try:
            result = close_resources(api, session_dir)
        except (Refusal, TransportUncertain) as error:
            event(session_dir, "close-attempt-unverified", error_type=type(error).__name__)
            result = None
        durable_write(
            session_dir / "watchdog-ready.json",
            {
                "session_id": session_dir.name,
                "pid": os.getpid(),
                "heartbeat_at": iso(utc_now()),
                "lock_owned": True,
                "closing": True,
            },
        )
        if isinstance(result, dict) and result.get("green") is True:
            return 0
        if isinstance(result, dict):
            resources_absent = bool(
                result.get("pod_get_404_and_full_list_absent")
                and result.get("volume_get_404_and_list_absent")
            )
            life = lifecycle(session_dir)
            reconcile_until = life.get("billing_reconcile_until")
            if (
                resources_absent
                and reconcile_until
                and utc_now() >= parse_time(reconcile_until, "billing reconciliation deadline")
            ):
                event(session_dir, "billing-still-unverified-after-bounded-reconciliation")
                return 3
        # If a pod or volume may remain, do not let the host backstop silently stop
        # trying merely because the planned two-hour work window has ended.
        time.sleep(poll_seconds)


def watch_loop(api: RunPodV2, session_dir: Path, *, poll_seconds: float = 10.0) -> int:
    identity = session_identity(session_dir)
    with exclusive_lock(session_dir / "watchdog.lock", blocking=False):
        started = time.monotonic()
        while True:
            durable_write(
                session_dir / "watchdog-ready.json",
                {
                    "session_id": identity["session_id"],
                    "pid": os.getpid(),
                    "heartbeat_at": iso(utc_now()),
                    "lock_owned": True,
                },
            )
            life = lifecycle(session_dir)
            if (session_dir / "stop.requested.json").exists():
                return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
            if life.get("cleanup_at") and utc_now() >= parse_time(life["cleanup_at"], "cleanup deadline"):
                return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
            if (
                life.get("pod_create_attempted")
                and life.get("pod_create_outcome") != "rejected"
                and not life.get("pod_id")
            ):
                authorization = life.get("authorization_public")
                if isinstance(authorization, dict):
                    deadlines = {
                        "hard_deadline_epoch": life["hard_deadline_epoch"],
                        "cleanup_epoch": life["cleanup_epoch"],
                    }
                    try:
                        reconcile_pod(api, session_dir, identity, authorization, deadlines, load_api_key())
                    except Refusal:
                        return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
            if (
                life.get("volume_create_attempted")
                and life.get("volume_create_outcome") != "rejected"
                and not life.get("volume_id")
            ):
                authorization = life.get("authorization_public")
                if isinstance(authorization, dict):
                    try:
                        reconcile_volume(api, session_dir, identity, authorization)
                    except Refusal:
                        return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
            pod_id = life.get("pod_id")
            if isinstance(pod_id, str) and pod_id:
                try:
                    status, pod = api.get_pod(pod_id)
                except (Refusal, TransportUncertain) as error:
                    event(
                        session_dir,
                        "pod-status-unverified",
                        pod_id=pod_id,
                        error_type=type(error).__name__,
                    )
                    time.sleep(poll_seconds)
                    continue
                if status == 404 or (pod is not None and pod.get("status") in {"ERROR", "EXITED", "TERMINATED"}):
                    return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
                if (
                    not life.get("runtime_acknowledged")
                    and life.get("pod_created_at")
                    and utc_now() - parse_time(life["pod_created_at"], "pod createdAt")
                    > dt.timedelta(seconds=PROVISIONING_TIMEOUT_SECONDS)
                ):
                    return close_until_bounded(api, session_dir, poll_seconds=poll_seconds)
            if not life.get("create_window_started_at") and time.monotonic() - started > 15 * 60:
                event(session_dir, "watchdog-expired-before-launch")
                return 2
            time.sleep(poll_seconds)


def auth_template(identity: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": SCHEMA_AUTHORIZATION,
        "authorize_paid_actions": False,
        "session_id": identity["session_id"],
        "action": "create-one-temporary-volume-and-one-gpu-pod",
        "authorized_at": "REQUIRED_CURRENT_RFC3339",
        "expires_at": "REQUIRED_WITHIN_ONE_HOUR_RFC3339",
        "acknowledges_combined_hourly_above_usd_1": False,
        "image": PINNED_IMAGE,
        "image_config_digest": PINNED_IMAGE_CONFIG,
        "gpu_id": "NVIDIA A100 80GB PCIe",
        "gpu_count": 1,
        "cloud": "SECURE",
        "data_center_id": "REQUIRED_FRESH_QUOTED_DATA_CENTER",
        "quoted_gpu_hourly_usd": "1.59",
        "pod_disk_gb": 200,
        "quoted_pod_disk_hourly_usd": "0.028",
        "expected_pod_hourly_usd": "1.618",
        "max_pod_hourly_usd": "REQUIRED_EXACT_CEILING",
        "volume_size_gb": 300,
        "volume_type": "STANDARD",
        "expected_volume_hourly_usd": "0.029",
        "max_volume_hourly_usd": "REQUIRED_EXACT_CEILING",
        "max_combined_hourly_usd": "REQUIRED_EXACT_CEILING",
        "planning_cap_seconds": 7200,
        "retrieval_margin_seconds": 900,
        "cleanup_margin_seconds": 600,
        "billing_cutoff_margin_seconds": 300,
        "volume_disposition": "delete-after-evidence-retrieval-or-at-cleanup-deadline",
        "merged_repository_commit": "REQUIRED_MERGED_FULL_SHA",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="create a durable random session; no provider call")
    prepare.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    preview = sub.add_parser("preview", help="validate and print a local authorization preview")
    preview.add_argument("--session-dir", type=Path, required=True)
    preview.add_argument("--authorization", type=Path)
    watch = sub.add_parser("watch", help="hold the watchdog lock and supervise one session")
    watch.add_argument("--session-dir", type=Path, required=True)
    launch_cmd = sub.add_parser("launch", help="perform the exact authorized one-shot paid creates")
    launch_cmd.add_argument("--session-dir", type=Path, required=True)
    launch_cmd.add_argument("--authorization", type=Path, required=True)
    launch_cmd.add_argument("--execute-exact-session", required=True)
    launch_cmd.add_argument("--i-understand-this-creates-billable-resources", action="store_true")
    ack = sub.add_parser("runtime-ack", help="validate a retrieved pod receipt and prepare its ack")
    ack.add_argument("--session-dir", type=Path, required=True)
    ack.add_argument("--receipt", type=Path, required=True)
    ack.add_argument("--output", type=Path, required=True)
    stop = sub.add_parser("stop", help="durably ask the prearmed watchdog to close and clean up")
    stop.add_argument("--session-dir", type=Path, required=True)
    reconcile = sub.add_parser("reconcile-close", help="take over only if watchdog is absent, then close")
    reconcile.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            session_dir = prepare_session(args.state_root)
            identity = session_identity(session_dir)
            durable_write(session_dir / "authorization-template.json", auth_template(identity), exclusive=True)
            print(json.dumps({"session_dir": str(session_dir), "paid_actions": "disabled"}))
            return 0
        if args.command == "preview":
            identity = session_identity(args.session_dir)
            if args.authorization is None:
                print(json.dumps(auth_template(identity), indent=2))
            else:
                print(json.dumps(authorization_preview(read_json(args.authorization, "authorization"), identity), indent=2))
            return 0
        if args.command == "stop":
            session_identity(args.session_dir)
            durable_touch(args.session_dir / "stop.requested.json")
            print(json.dumps({"session_id": args.session_dir.name, "stop_requested": True}))
            return 0
        if args.command == "runtime-ack":
            ack = record_runtime_ack(args.session_dir, args.receipt, args.output)
            print(json.dumps({k: v for k, v in ack.items() if k != "controller_challenge"}, indent=2))
            return 0
        if args.command == "launch":
            identity = session_identity(args.session_dir)
            if args.execute_exact_session != identity["session_id"]:
                raise Refusal("--execute-exact-session does not equal this exact random session")
            if not args.i_understand_this_creates_billable_resources:
                raise Refusal("literal billable-resource execution flag is absent")
            authorization = read_json(args.authorization, "authorization")
            api_key = load_api_key()
            result = launch(RunPodV2(UrllibTransport(api_key)), args.session_dir, authorization, api_key)
            print(json.dumps(result, indent=2))
            return 0 if result.get("green") else 3
        if args.command == "watch":
            api_key = load_api_key()
            return watch_loop(RunPodV2(UrllibTransport(api_key)), args.session_dir)
        if args.command == "reconcile-close":
            try:
                with exclusive_lock(args.session_dir / "watchdog.lock", blocking=False):
                    api_key = load_api_key()
                    result = close_resources(RunPodV2(UrllibTransport(api_key)), args.session_dir)
            except BlockingIOError as error:
                raise Refusal("live watchdog still owns this session; use the durable stop flag") from error
            print(json.dumps(result, indent=2))
            return 0 if result["green"] else 3
        raise AssertionError(args.command)
    except Refusal as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    except TransportUncertain as error:
        print(f"UNVERIFIED PROVIDER OUTCOME: {type(error).__name__}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("INTERRUPTED: inspect the durable session and reconcile before any new paid action", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
