#!/usr/bin/env python3
"""Remote, read-only collector and tightly-scoped app job executor."""
import base64
import json
import math
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor

REQUEST = {}  # Replaced with a base64-encoded JSON literal by the local bridge.


def midclt(method, *args, timeout=18):
    result = subprocess.run(
        ["midclt", "call", method, *[json.dumps(arg, separators=(",", ":")) if isinstance(arg, (dict, list, bool)) or arg is None else str(arg) for arg in args]],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "midclt failed").strip()[:400])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("midclt returned invalid JSON") from exc


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def rows(result):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("data", "results", "items"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def latest_point(result, legend=None, aggregate=False):
    """Read the latest data column selected by its exact reporting legend."""
    candidates = []
    for graph in rows(result):
        if not isinstance(graph, dict):
            continue
        legends = graph.get("legend")
        if isinstance(legends, str):
            legends = [legends]
        if not isinstance(legends, (list, tuple)):
            legends = [graph.get("name")] if graph.get("name") else []
        names = [str(item).strip().lower() if item is not None else "" for item in legends]
        if legend is not None:
            indexes = [i for i, name in enumerate(names) if name == legend.lower()]
        elif aggregate:
            indexes = [i for i, name in enumerate(names) if name != "time"]
        else:
            indexes = []
        if not indexes:
            continue
        for point in graph.get("data", []) if isinstance(graph.get("data"), list) else []:
            if not isinstance(point, (list, tuple)) or not point:
                continue
            timestamp = number(point[0])
            if timestamp is None:
                continue
            for index in indexes:
                column = index if names and names[0] == "time" else index + 1
                if column < len(point):
                    value = number(point[column])
                    if value is not None:
                        candidates.append((timestamp, value))
    if not candidates:
        return None
    newest = max(timestamp for timestamp, _ in candidates)
    values = [value for timestamp, value in candidates if timestamp == newest]
    return sum(values) / len(values) if aggregate else values[-1]


def portal_url(*values):
    """Return the first safe HTTP(S) URL from explicit portal fields only."""
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for preferred in ("url", "portal", "web", "http", "https"):
                if preferred in value:
                    yield from strings(value[preferred])
            for key, item in value.items():
                if key not in ("url", "portal", "web", "http", "https"):
                    yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from strings(item)

    for value in values:
        for candidate in strings(value):
            try:
                parsed = urlsplit(candidate.strip())
                hostname = parsed.hostname
                if parsed.scheme.lower() not in ("http", "https") or not hostname or parsed.username or parsed.password:
                    continue
                host_lower = hostname.lower()
                if host_lower in ("0.0.0.0", "::", "[::]", "*", "localhost"):
                    continue
                if len(candidate) > 300:
                    continue
                return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
            except ValueError:
                continue
    return None


def graph(name, start, end):
    return midclt("reporting.get_data", [{"name": name}], {"start": start, "end": end, "aggregate": False})


def collect(include_inventory=False):
    now = int(time.time())
    errors = []
    jobs = {
        "system": lambda: midclt("system.info"),
        "cpu": lambda: graph("cpu", now - 90, now),
        "memory": lambda: graph("memory", now - 90, now),
        "temperature": lambda: graph("cputemp", now - 90, now),
        "arc": lambda: graph("arcsize", now - 90, now),
    }
    if include_inventory:
        jobs.update({
            "apps": lambda: midclt("app.query"),
            "pools": lambda: midclt("pool.query"),
            "alerts": lambda: midclt("alert.list"),
        })
    results = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {key: executor.submit(call) for key, call in jobs.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result(timeout=20)
            except Exception as exc:
                results[key] = None
                errors.append(f"{key}: {str(exc)[:180]}")
    system = results.get("system") or {}
    if not isinstance(system, dict):
        system = {}
    apps_out = []
    for app in rows(results.get("apps"))[:100]:
        if not isinstance(app, dict):
            continue
        name = app.get("name") or app.get("id")
        if not isinstance(name, str):
            continue
        version = app.get("version")
        update = app.get("update_available", app.get("upgrade_available"))
        portal = portal_url(app.get("portal"), app.get("portals"))
        apps_out.append({
            "name": name[:80], "state": str(app.get("state", app.get("status", "UNKNOWN"))).upper()[:32],
            "version": str(version)[:80] if version is not None else None,
            "update_available": bool(update) if update is not None else None,
            "portal": str(portal)[:300] if portal else None,
        })
    pools_out = []
    for pool in rows(results.get("pools"))[:32]:
        if not isinstance(pool, dict):
            continue
        name = pool.get("name")
        if not isinstance(name, str):
            continue
        size, allocated, free = (number(pool.get(k)) for k in ("size", "allocated", "free"))
        percent = allocated / size * 100 if size and allocated is not None else None
        healthy = pool.get("healthy")
        pools_out.append({
            "name": name[:80], "status": str(pool.get("status", "UNKNOWN"))[:32],
            "healthy": bool(healthy) if healthy is not None else None,
            "size": size, "allocated": allocated, "free": free,
            "percent": round(percent, 2) if percent is not None else None,
        })
    alerts_out = []
    for alert in rows(results.get("alerts")):
        if not isinstance(alert, dict) or alert.get("dismissed") is True:
            continue
        if len(alerts_out) >= 50:
            break
        alerts_out.append({
            "id": str(alert.get("uuid", alert.get("id", "")))[:100],
            "level": str(alert.get("level", alert.get("severity", "INFO"))).upper()[:24],
            "message": str(alert.get("formatted", alert.get("text", alert.get("message", ""))))[:500],
        })
    interface = REQUEST.get("interface", "br1")
    rx = tx = None
    try:
        with open("/proc/net/dev", "r", encoding="ascii") as stream:
            for line in stream:
                if ":" not in line:
                    continue
                iface, values = line.split(":", 1)
                if iface.strip() == interface:
                    counters = values.split()
                    rx, tx = int(counters[0]), int(counters[8])
                    break
    except (OSError, ValueError, IndexError):
        errors.append("network: unable to read selected interface counters")
    if results.get("system") is None and all(results.get(key) is None for key in ("cpu", "memory", "temperature", "arc")) and rx is None:
        raise RuntimeError("system and metrics collection failed")
    memtotal = number(system.get("physmem"))
    memavailable = latest_point(results.get("memory"), "available")
    memused = max(0, memtotal - memavailable) if memtotal is not None and memavailable is not None else None
    temperature = latest_point(results.get("temperature"), "cpu")
    if temperature is None:
        temperature = latest_point(results.get("temperature"), aggregate=True)
    data = {
        "server": {
            "hostname": str(system.get("hostname"))[:120] if system.get("hostname") else None,
            "version": str(system.get("version"))[:120] if system.get("version") else None,
            "uptime_seconds": number(system.get("uptime_seconds", system.get("uptime"))),
            "total_memory": memtotal,
        },
        "metrics": {
            "cpu_percent": latest_point(results.get("cpu"), "cpu"),
            "memory_used": memused, "memory_total": memtotal,
            "memory_percent": round(memused / memtotal * 100, 2) if memused is not None and memtotal else None,
            "temperature_c": temperature, "arc_bytes": latest_point(results.get("arc"), "size"),
            "network_rx_bytes": rx, "network_tx_bytes": tx,
            "network_interface": interface, "network_timestamp": now if rx is not None else None,
        },
        "observed_at": now, "errors": errors,
        "truncated": False,
    }
    if include_inventory:
        if results.get("apps") is not None:
            data["apps"] = apps_out
        if results.get("pools") is not None:
            data["pools"] = pools_out
        if results.get("alerts") is not None:
            data["alerts"] = alerts_out
        data["truncated"] = (
            len(rows(results.get("apps"))) > 100
            or len(rows(results.get("pools"))) > 32
            or len(rows(results.get("alerts"))) > 50
        )
    return data


def app_by_name(name, timeout=18):
    for app in rows(midclt("app.query", timeout=timeout)):
        if isinstance(app, dict) and app.get("name") == name:
            return app
    return None


def wait_job(job_id, deadline):
    job_id = int(job_id)
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            jobs = midclt("core.get_jobs", [[("id", "=", job_id)]], timeout=max(1, min(5, remaining - 0.5)))
        except Exception as exc:
            return {"job_id": job_id, "state": "UNKNOWN", "error": str(exc)[:300]}
        found = next((j for j in rows(jobs) if isinstance(j, dict) and int(j.get("id", -1)) == job_id), None)
        if found:
            state = str(found.get("state", "")).upper()
            if state in ("SUCCESS", "FAILED", "ABORTED"):
                return {"job_id": job_id, "state": state, "error": str(found.get("error") or "")[:400]}
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1, remaining))
    return {"job_id": job_id, "state": "RUNNING", "error": "job is still running"}


def launch(app, action, deadline):
    # Only fixed middleware methods and a validated, exact app name reach midclt.
    remaining = deadline - time.monotonic()
    if remaining < 2:
        raise TimeoutError("operation deadline reached before launching middleware job")
    result = midclt("app." + action, app, timeout=max(1, min(10, remaining - 1)))
    if isinstance(result, int):
        job_id = result
    elif isinstance(result, dict) and isinstance(result.get("job_id", result.get("id")), int):
        job_id = result.get("job_id", result.get("id"))
    else:
        raise RuntimeError("middleware did not return a numeric job id")
    try:
        return wait_job(job_id, deadline)
    except Exception as exc:
        return {"job_id": job_id, "state": "UNKNOWN", "error": str(exc)[:300]}


def main():
    op = REQUEST.get("op")
    app = REQUEST.get("app")
    if op in ("snapshot", "inventory"):
        print(json.dumps({"ok": True, "data": collect(include_inventory=op == "inventory")}, separators=(",", ":")))
        return
    if op == "reconcile":
        pending = REQUEST.get("pending")
        if not isinstance(pending, dict) or not isinstance(pending.get("job_id"), int):
            print(json.dumps({"ok": False, "error": "no recoverable middleware job id"}, separators=(",", ":")))
            return
        status = wait_job(pending["job_id"], time.monotonic() + 3)
        phase = pending.get("phase")
        data = {"job": status, "phase": phase, "stop_job_id": pending.get("stop_job_id")}
        print(json.dumps({"ok": status["state"] == "SUCCESS", "data": data, "error": status.get("error", "")}, separators=(",", ":")))
        return
    if op not in ("start", "stop", "restart") or not isinstance(app, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", app):
        print(json.dumps({"ok": False, "error": "invalid operation or app name"}, separators=(",", ":")))
        return
    budget = number(REQUEST.get("timeout", 110))
    if budget is None or budget <= 0 or budget > 180:
        print(json.dumps({"ok": False, "error": "invalid action timeout"}, separators=(",", ":")))
        return
    deadline = time.monotonic() + budget
    phase = "stop" if op == "restart" else op
    stop_job = None
    launch_attempted = False
    try:
        remaining = deadline - time.monotonic()
        if remaining < 2:
            print(json.dumps({"ok": False, "error": "operation deadline reached before app lookup"}, separators=(",", ":")))
            return
        current = app_by_name(app, timeout=max(1, min(5, remaining - 1)))
        if current is None:
            print(json.dumps({"ok": False, "error": "app not found"}, separators=(",", ":")))
            return
        if op == "restart":
            if deadline - time.monotonic() < 2:
                print(json.dumps({"ok": False, "unknown": False, "error": "operation deadline reached before stop launch"}, separators=(",", ":")))
                return
            launch_attempted = True
            stop_job = launch(app, "stop", deadline)
            if stop_job["state"] != "SUCCESS":
                state = stop_job["state"]
                print(json.dumps({"ok": False, "unknown": state not in ("FAILED", "ABORTED"), "error": stop_job.get("error", "stop did not complete"), "data": {"phase": "stop", "job": stop_job}}, separators=(",", ":")))
                return
            phase = "start"
            if deadline - time.monotonic() < 2:
                print(json.dumps({"ok": False, "unknown": False, "error": "stop succeeded; restart incomplete; app may remain stopped", "data": {"phase": "stop", "job": stop_job}}, separators=(",", ":")))
                return
            launch_attempted = True
        else:
            if deadline - time.monotonic() < 2:
                print(json.dumps({"ok": False, "unknown": False, "error": "operation deadline reached before job launch"}, separators=(",", ":")))
                return
            launch_attempted = True
        job = launch(app, phase if op != "restart" else "start", deadline)
        data = {"phase": phase, "job": job}
        if stop_job is not None:
            data["stop_job"] = stop_job
        if job["state"] == "SUCCESS":
            print(json.dumps({"ok": True, "data": data}, separators=(",", ":")))
        else:
            error = job.get("error", "job outcome is not yet known")
            if op == "restart" and job["state"] in ("FAILED", "ABORTED"):
                error = (error + "; restart incomplete; app may remain stopped").strip("; ")
            print(json.dumps({"ok": False, "unknown": job["state"] not in ("FAILED", "ABORTED"), "error": error, "data": data}, separators=(",", ":")))
    except Exception as exc:
        data = {"phase": phase}
        if stop_job is not None:
            data["stop_job"] = stop_job
        error = str(exc)[:300]
        if op == "restart" and stop_job is not None and phase == "start":
            error += "; restart incomplete; app may remain stopped"
        print(json.dumps({"ok": False, "unknown": launch_attempted, "error": error, "data": data}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "unknown": True, "error": str(exc)[:400]}, separators=(",", ":")))
