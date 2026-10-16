#!/usr/bin/env python3
"""Small stdlib-only, SSH-based TrueNAS data and app-control bridge."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

REMOTE_SOURCE = Path(__file__).with_name("remote.py")
APP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
USER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


class BridgeError(Exception):
    pass


def validate_host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253 or any(c in host for c in "\r\n\0 @") or host.startswith("-"):
        raise BridgeError("invalid host")
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        if not HOST_RE.fullmatch(host):
            raise BridgeError("invalid host")
        return host


def validate_identity(identity: str) -> str:
    if not isinstance(identity, str) or any(c in identity for c in "\r\n\0"):
        raise BridgeError("invalid identity path")
    if not identity:
        return ""
    path = Path(identity).expanduser()
    if not path.is_absolute():
        raise BridgeError("identity path must be absolute")
    return str(path)


def endpoint_key(args) -> str:
    identity = args.identity or ""
    endpoint = "\0".join((args.host, args.user, str(args.port), identity))
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]


def state_files(args):
    directory = Path(args.state_dir).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    key = endpoint_key(args)
    return directory, directory / (key + ".json"), directory / (key + ".lock")


def read_state(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise BridgeError("local operation journal is unreadable; inspect state directory")
    if not isinstance(value, dict):
        raise BridgeError("local operation journal has an invalid root type")
    if "pending" not in value:
        raise BridgeError("local operation journal is missing its pending field")
    pending = value["pending"]
    if pending is None:
        return value
    if not isinstance(pending, dict):
        raise BridgeError("local operation journal has an invalid pending operation")
    if pending.get("op") not in ("start", "stop", "restart") or not APP_RE.fullmatch(str(pending.get("app", ""))):
        raise BridgeError("local operation journal has an invalid pending operation")
    if pending.get("phase") not in ("start", "stop"):
        raise BridgeError("local operation journal has an invalid pending phase")
    for field in ("job_id", "stop_job_id"):
        job_id = pending.get(field)
        if job_id is not None and (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 0):
            raise BridgeError("local operation journal has an invalid job id")
    op = pending["op"]
    phase = pending["phase"]
    if (op == "start" and phase != "start") or (op == "stop" and phase != "stop"):
        raise BridgeError("local operation journal has an inconsistent operation phase")
    if op == "restart" and phase == "start" and pending.get("stop_job_id") is None:
        raise BridgeError("local operation journal is missing the completed stop job")
    request_id = pending.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)):
        raise BridgeError("local operation journal has an invalid request id")
    return value


def write_state(path: Path, state):
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remote_request(args, request):
    """Run a fixed remote Python program with JSON embedded as base64 source."""
    source = REMOTE_SOURCE.read_text(encoding="utf-8")
    marker = "REQUEST = {}  # Replaced with a base64-encoded JSON literal by the local bridge."
    if source.count(marker) != 1:
        raise BridgeError("remote collector template is invalid")
    encoded = base64.b64encode(json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).decode("ascii")
    source = source.replace(marker, f'REQUEST = json.loads(base64.b64decode("{encoded}").decode("utf-8"))')
    command = ["ssh", "-T", "-p", str(args.port),
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
               "-o", "ServerAliveCountMax=2"]
    if args.identity:
        command.extend(["-i", args.identity])
    destination_host = f"[{args.host}]" if ":" in args.host else args.host
    command.extend([f"{args.user}@{destination_host}", "python3", "-I", "-"])
    try:
        result = subprocess.run(command, input=source, capture_output=True, text=True,
                                timeout=args.timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("SSH request timed out; a remote mutation may still be running") from exc
    except OSError as exc:
        raise BridgeError(f"unable to start SSH: {exc}") from exc
    output = (result.stdout or "")
    payload = None
    # SSH startup and remote interpreter diagnostics go to stderr. Parse only the
    # final nonempty stdout line so the bridge always emits its own single JSON.
    for line in reversed(output.splitlines()):
        if len(line) > 120_000:
            continue
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict) and isinstance(candidate.get("ok"), bool):
                payload = candidate
                break
        except ValueError:
            continue
    if payload is None:
        detail = (result.stderr or "remote returned no JSON response").strip().replace("\n", " ")[:300]
        raise ConnectionError(detail)
    return payload, result.returncode


def response(ok, *, error="", unknown=False, job_id=None, data=None, pending=None):
    out = {"ok": bool(ok), "error": str(error)[:500], "unknown": bool(unknown)}
    if job_id is not None:
        out["job_id"] = job_id
    if data is not None:
        out["data"] = data
    out["pending"] = pending
    return out


def execute_locked(args, journal_path):
    state = read_state(journal_path)
    pending = state.get("pending")
    request_id = args.request_id
    if request_id and state.get("last_request_id") == request_id and isinstance(state.get("last_result"), dict):
        return state["last_result"]
    if args.op in ("snapshot", "inventory"):
        try:
            payload, _ = remote_request(args, {"op": args.op, "interface": args.interface})
            if not payload.get("ok"):
                return response(False, error=payload.get("error", "remote collection failed"), pending=pending)
            return response(True, data=payload.get("data", {}), pending=pending)
        except Exception as exc:
            return response(False, error=str(exc), unknown=False, pending=pending)
    if args.op == "reconcile":
        if not pending:
            return response(True, data={"reconciled": False, "message": "no operation is pending"}, pending=None)
        if pending.get("job_id") is None:
            return response(False, error="No middleware job ID was captured; automatic reconciliation is unavailable. Controls remain locked. An operator must establish the exact job outcome in TrueNAS; app state alone is insufficient. Do not delete the journal to retry.", unknown=True, pending=pending)
        try:
            payload, _ = remote_request(args, {"op": "reconcile", "pending": pending, "interface": args.interface})
            job = (payload.get("data") or {}).get("job")
            if not isinstance(job, dict):
                return response(False, error=payload.get("error", "pending operation cannot be reconciled"), unknown=True, job_id=pending.get("job_id"), pending=pending)
            state_name = str(job.get("state", "")).upper()
            if state_name in ("SUCCESS", "FAILED", "ABORTED"):
                phase = (payload.get("data") or {}).get("phase", pending.get("phase"))
                restart_stopped = pending.get("op") == "restart" and phase == "stop" and state_name == "SUCCESS"
                error = (job.get("error") or f"middleware job {state_name.lower()}") if state_name != "SUCCESS" else ""
                if restart_stopped:
                    error = "stop succeeded; restart incomplete; app is stopped"
                elif pending.get("op") == "restart" and phase == "start" and state_name != "SUCCESS":
                    error = (error + "; restart incomplete; app may remain stopped").strip("; ")
                result = response(state_name == "SUCCESS" and not restart_stopped, error=error, job_id=job.get("job_id"), data={"job": job, "phase": phase, "reconciled": True, "restart_incomplete": restart_stopped or (pending.get("op") == "restart" and state_name != "SUCCESS")}, pending=None)
                state["pending"] = None
                state["last_request_id"] = pending.get("request_id")
                state["last_result"] = result
                write_state(journal_path, state)
                return result
            return response(False, error="middleware job is still running; retry reconcile", unknown=True, job_id=job.get("job_id"), data={"job": job}, pending=pending)
        except Exception as exc:
            return response(False, error=str(exc), unknown=True, job_id=pending.get("job_id"), pending=pending)
    if args.op not in ("start", "stop", "restart"):
        return response(False, error="unsupported operation", pending=pending)
    if not APP_RE.fullmatch(args.app or ""):
        return response(False, error="invalid app name", pending=pending)
    if pending:
        return response(False, error="another app operation has an unresolved outcome; run reconcile first", unknown=True, job_id=pending.get("job_id"), pending=pending)
    initial_phase = "stop" if args.op == "restart" else args.op
    intent = {"app": args.app, "op": args.op, "phase": initial_phase,
              "request_id": request_id, "started_at": time.time(), "job_id": None,
              "stop_job_id": None}
    state["pending"] = intent
    write_state(journal_path, state)
    try:
        remote_budget = max(1, args.timeout - 8)
        payload, _ = remote_request(args, {"op": args.op, "app": args.app,
                                           "interface": args.interface, "timeout": remote_budget})
    except Exception as exc:
        # SSH may disconnect after middleware accepted the action. Retain the intent.
        return response(False, error=str(exc), unknown=True, pending=intent)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    phase = data.get("phase")
    if phase in ("start", "stop"):
        intent["phase"] = phase
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_id = job.get("job_id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        intent["job_id"] = job_id
    stop_job = data.get("stop_job") if isinstance(data.get("stop_job"), dict) else None
    if stop_job and isinstance(stop_job.get("job_id"), int):
        intent["stop_job_id"] = stop_job["job_id"]
    elif args.op == "restart" and intent["phase"] == "stop" and isinstance(job_id, int):
        intent["stop_job_id"] = job_id
    job_state = str(job.get("state", "")).upper()
    if payload.get("ok") and job_state == "SUCCESS" and (args.op != "restart" or intent["phase"] == "start"):
        result = response(True, job_id=job_id, data=data)
        state["pending"] = None
        state["last_request_id"] = request_id
        state["last_result"] = result
        write_state(journal_path, state)
        return result
    if (job_state in ("FAILED", "ABORTED")
            or (not payload.get("ok") and not job_state and not payload.get("unknown"))
            or (args.op == "restart" and intent["phase"] == "stop" and job_state == "SUCCESS" and not payload.get("ok") and not payload.get("unknown"))):
        error = payload.get("error") or job.get("error") or f"app operation {job_state.lower() or 'failed'}"
        if args.op == "restart" and (intent["phase"] == "start" or (intent["phase"] == "stop" and job_state == "SUCCESS")):
            error = (str(error) + "; restart incomplete; app may remain stopped").strip("; ")
        result = response(False, error=error, job_id=job_id, data=data)
        state["pending"] = None
        state["last_request_id"] = request_id
        state["last_result"] = result
        write_state(journal_path, state)
        return result
    write_state(journal_path, state)
    return response(False, error=payload.get("error") or "app operation outcome is not yet known", unknown=True, job_id=job_id, data=data, pending=intent)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", default="")
    parser.add_argument("--interface", default="br1")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("op", choices=("snapshot", "inventory", "start", "stop", "restart", "reconcile"))
    parser.add_argument("--app")
    parser.add_argument("--request-id")
    return parser


def run(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        args.host = validate_host(args.host)
        if not USER_RE.fullmatch(args.user):
            raise BridgeError("invalid username")
        if not 1 <= args.port <= 65535:
            raise BridgeError("invalid SSH port")
        if not 1 <= args.timeout <= 180:
            raise BridgeError("timeout must be between 1 and 180 seconds")
        args.identity = validate_identity(args.identity)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", args.interface):
            raise BridgeError("invalid network interface")
        if args.request_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", args.request_id):
            raise BridgeError("invalid request id")
        directory, journal, lock_path = state_files(args)
        del directory
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            return execute_locked(args, journal)
        finally:
            os.close(fd)
    except SystemExit:
        raise
    except Exception as exc:
        return response(False, error=str(exc))


def main():
    try:
        result = run()
    except SystemExit as exc:
        # argparse diagnostics are deliberately not printed to stdout. A CLI usage
        # error still conforms to the one-line JSON response contract.
        result = response(False, error="invalid command line arguments")
        print(json.dumps(result, separators=(",", ":")))
        raise SystemExit(int(exc.code or 0))
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
