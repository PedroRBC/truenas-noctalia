import base64
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "truenas" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import truenas_bridge as bridge


class BridgeValidationTests(unittest.TestCase):
    def test_host_validation_accepts_dns_and_ip(self):
        self.assertEqual(bridge.validate_host("nas.example.test"), "nas.example.test")
        self.assertEqual(bridge.validate_host("192.168.99.114"), "192.168.99.114")
        self.assertEqual(bridge.validate_host("[2001:db8::2]"), "2001:db8::2")

    def test_host_validation_rejects_option_injection_and_controls(self):
        for host in ("-oProxyCommand=bad", "user@host", "nas\nthing", "bad host", ""):
            with self.subTest(host=host), self.assertRaises(bridge.BridgeError):
                bridge.validate_host(host)

    def test_identity_requires_absolute_path_and_allows_empty(self):
        self.assertEqual(bridge.validate_identity(""), "")
        self.assertEqual(bridge.validate_identity("~/keys/nas"), str(Path.home() / "keys/nas"))
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_identity("relative/key")
        with self.assertRaises(bridge.BridgeError):
            bridge.validate_identity("/tmp/key\n-oProxyCommand=bad")

    def test_cli_validation_and_contract(self):
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.object(bridge, "remote_request", side_effect=bridge.BridgeError("SSH unavailable")):
                result = bridge.run(["--host", "127.0.0.1", "--user", "root", "--port", "22",
                                     "--identity", "", "--interface", "br1", "--timeout", "20",
                                     "--state-dir", state, "snapshot"])
        self.assertTrue({"ok", "error", "unknown", "pending"}.issubset(result))
        self.assertLessEqual(set(result), {"ok", "error", "unknown", "job_id", "data", "pending"})
        self.assertFalse(result["ok"])
        self.assertIn("SSH", result["error"])

    def test_invalid_timeout_and_interface_never_start_ssh(self):
        with mock.patch.object(bridge, "remote_request") as remote:
            result = bridge.run(["--host", "nas", "--state-dir", "/tmp/bridge-tests", "--timeout", "181", "snapshot"])
            self.assertFalse(result["ok"])
            remote.assert_not_called()


class RemoteInvocationTests(unittest.TestCase):
    def args(self, state, op="snapshot", *extra):
        return bridge.build_parser().parse_args(["--host", "192.168.99.114", "--user", "root", "--port", "22",
                                                  "--identity", "", "--interface", "br1", "--timeout", "20",
                                                  "--state-dir", str(state), *extra, op])

    def test_remote_uses_ssh_without_shell_and_base64_request_source(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state)
            args.host = bridge.validate_host(args.host)
            request = {"op": "snapshot", "interface": "br1", "app": "x\"; id"}
            completed = subprocess.CompletedProcess([], 0, stdout='{"ok":true,"data":{}}\n', stderr="")
            with mock.patch.object(bridge.subprocess, "run", return_value=completed) as call:
                payload, rc = bridge.remote_request(args, request)
            self.assertEqual(payload, {"ok": True, "data": {}})
            self.assertEqual(rc, 0)
            cmd = call.call_args.args[0]
            self.assertNotIn("-oProxyCommand=bad", cmd)
            self.assertIn("StrictHostKeyChecking=yes", cmd)
            self.assertEqual(call.call_args.kwargs["shell"] if "shell" in call.call_args.kwargs else False, False)
            source = call.call_args.kwargs["input"]
            self.assertNotIn(request["app"], source)
            encoded = source.split('REQUEST = json.loads(base64.b64decode("', 1)[1].split('"', 1)[0]
            self.assertEqual(json.loads(base64.b64decode(encoded)), request)

    def test_remote_timeout_reports_uncertain_mutation(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "start")
            with mock.patch.object(bridge.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 20)):
                with self.assertRaises(TimeoutError):
                    bridge.remote_request(args, {"op": "start", "app": "jellyfin"})


class MutationTests(unittest.TestCase):
    def args(self, state, op, app="jellyfin", request_id=None):
        raw = ["--host", "nas.example.test", "--state-dir", str(state), op]
        if app is not None:
            raw.extend(["--app", app])
        if request_id:
            raw.extend(["--request-id", request_id])
        parsed = bridge.build_parser().parse_args(raw)
        parsed.host = bridge.validate_host(parsed.host)
        parsed.identity = bridge.validate_identity(parsed.identity)
        return parsed

    def test_successful_action_clears_pending_and_caches_request_id(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "start", request_id="req-1")
            payload = {"ok": True, "data": {"job": {"job_id": 41, "state": "SUCCESS"}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)) as remote:
                first = bridge.execute_locked(args, bridge.state_files(args)[1])
                again = bridge.execute_locked(args, bridge.state_files(args)[1])
            self.assertTrue(first["ok"])
            self.assertEqual(first["job_id"], 41)
            self.assertEqual(again, first)
            self.assertEqual(remote.call_count, 1)
            self.assertIsNone(first["pending"])

    def test_timeout_keeps_pending_and_blocks_replay(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "restart", request_id="req-2")
            journal = bridge.state_files(args)[1]
            with mock.patch.object(bridge, "remote_request", side_effect=TimeoutError("timed out")) as remote:
                first = bridge.execute_locked(args, journal)
                second = bridge.execute_locked(self.args(state, "start", request_id="req-3"), journal)
            self.assertTrue(first["unknown"])
            self.assertEqual(first["pending"]["op"], "restart")
            self.assertTrue(second["unknown"])
            self.assertIn("reconcile", second["error"])
            self.assertEqual(remote.call_count, 1)

    def test_action_sends_deadline_budget_and_persists_phase_and_both_job_ids(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "restart")
            journal = bridge.state_files(args)[1]
            payload = {"ok": False, "unknown": True, "error": "start status unknown",
                       "data": {"phase": "start", "job": {"job_id": 22, "state": "UNKNOWN"},
                                "stop_job": {"job_id": 21, "state": "SUCCESS"}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)) as remote:
                result = bridge.execute_locked(args, journal)
            request = remote.call_args.args[1]
            self.assertEqual(request["timeout"], 12)
            pending = bridge.read_state(journal)["pending"]
            self.assertEqual(pending["phase"], "start")
            self.assertEqual(pending["job_id"], 22)
            self.assertEqual(pending["stop_job_id"], 21)
            self.assertTrue(result["unknown"])

    def test_known_stop_success_with_restart_incomplete_returns_failure(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "restart")
            journal = bridge.state_files(args)[1]
            payload = {"ok": False, "unknown": False,
                       "error": "stop succeeded; restart incomplete; app may remain stopped",
                       "data": {"phase": "stop", "job": {"job_id": 9, "state": "SUCCESS"}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)):
                result = bridge.execute_locked(args, journal)
            self.assertFalse(result["ok"])
            self.assertFalse(result["unknown"])
            self.assertIn("restart incomplete", result["error"])
            self.assertIsNone(bridge.read_state(journal)["pending"])

    def test_job_still_running_is_unknown_with_job_id(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "stop")
            journal = bridge.state_files(args)[1]
            payload = {"ok": True, "data": {"job": {"job_id": 7, "state": "RUNNING"}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)):
                result = bridge.execute_locked(args, journal)
            self.assertTrue(result["unknown"])
            self.assertEqual(result["job_id"], 7)
            self.assertEqual(bridge.read_state(journal)["pending"]["job_id"], 7)

    def test_remote_uncertain_error_is_journaled_but_definitive_error_clears(self):
        with tempfile.TemporaryDirectory() as state:
            journal = bridge.state_files(self.args(state, "start"))[1]
            args = self.args(state, "start")
            uncertain = {"ok": False, "unknown": True, "error": "middleware call timed out"}
            with mock.patch.object(bridge, "remote_request", return_value=(uncertain, 0)):
                result = bridge.execute_locked(args, journal)
            self.assertTrue(result["unknown"])
            self.assertIsNotNone(bridge.read_state(journal)["pending"])
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "start")
            journal = bridge.state_files(args)[1]
            failure = {"ok": False, "error": "app not found"}
            with mock.patch.object(bridge, "remote_request", return_value=(failure, 0)):
                result = bridge.execute_locked(args, journal)
            self.assertFalse(result["unknown"])
            self.assertIsNone(bridge.read_state(journal)["pending"])

    def test_reconcile_resolves_terminal_job(self):
        with tempfile.TemporaryDirectory() as state:
            pending_args = self.args(state, "start", request_id="req-4")
            journal = bridge.state_files(pending_args)[1]
            bridge.write_state(journal, {"pending": {"app": "jellyfin", "op": "start", "phase": "start", "request_id": "req-4", "job_id": 12, "stop_job_id": None}})
            reconcile = self.args(state, "reconcile", app=None)
            payload = {"ok": True, "data": {"job": {"job_id": 12, "state": "SUCCESS", "error": ""}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)):
                result = bridge.execute_locked(reconcile, journal)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["pending"])
            self.assertIsNone(bridge.read_state(journal)["pending"])

    def test_reconcile_with_unrecoverable_operation_keeps_pending(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "start")
            journal = bridge.state_files(args)[1]
            intent = {"app": "jellyfin", "op": "restart", "phase": "stop", "request_id": "r", "job_id": None, "stop_job_id": None}
            bridge.write_state(journal, {"pending": intent})
            reconcile = self.args(state, "reconcile", app=None)
            with mock.patch.object(bridge, "remote_request", return_value=({"ok": False, "error": "no recoverable middleware job id"}, 0)):
                result = bridge.execute_locked(reconcile, journal)
            self.assertTrue(result["unknown"])
            self.assertEqual(result["pending"], intent)
            self.assertIn("app state alone is insufficient", result["error"])
            self.assertEqual(bridge.read_state(journal)["pending"], intent)

    def test_failed_restart_start_reports_app_may_remain_stopped(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "restart")
            journal = bridge.state_files(args)[1]
            payload = {"ok": False, "error": "start failed; restart incomplete; app may remain stopped",
                       "data": {"phase": "start", "job": {"job_id": 22, "state": "FAILED", "error": "start failed"},
                                "stop_job": {"job_id": 21, "state": "SUCCESS", "error": ""}}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 1)):
                result = bridge.execute_locked(args, journal)
            self.assertFalse(result["ok"])
            self.assertIn("may remain stopped", result["error"])
            self.assertIsNone(bridge.read_state(journal)["pending"])

    def test_reconcile_success_of_restart_stop_is_not_restart_success(self):
        with tempfile.TemporaryDirectory() as state:
            action = self.args(state, "restart")
            journal = bridge.state_files(action)[1]
            pending = {"app": "jellyfin", "op": "restart", "phase": "stop", "request_id": "r1",
                       "started_at": 1, "job_id": 21, "stop_job_id": 21}
            bridge.write_state(journal, {"pending": pending})
            reconcile = self.args(state, "reconcile", app=None)
            payload = {"ok": True, "data": {"job": {"job_id": 21, "state": "SUCCESS", "error": ""}, "phase": "stop"}}
            with mock.patch.object(bridge, "remote_request", return_value=(payload, 0)):
                result = bridge.execute_locked(reconcile, journal)
            self.assertFalse(result["ok"])
            self.assertIn("restart incomplete", result["error"])
            self.assertTrue(result["data"]["restart_incomplete"])
            self.assertIsNone(result["pending"])

    def test_malformed_or_wrong_type_journal_fails_closed(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "start")
            journal = bridge.state_files(args)[1]
            for contents in ("not-json", "[]", '{"pending":[]} ',
                             '{"pending":{"op":"restart","phase":"start","app":"jellyfin","job_id":1}}'):
                journal.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(bridge.BridgeError):
                    bridge.read_state(journal)

    def test_journal_file_and_lock_are_private(self):
        with tempfile.TemporaryDirectory() as state:
            args = self.args(state, "snapshot")
            _, journal, lock = bridge.state_files(args)
            bridge.write_state(journal, {"pending": None})
            lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(lock_fd)
            self.assertEqual(os.stat(journal).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)


class RemoteModuleMixin:
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("remote_collector", SCRIPT_DIR / "remote.py")
        cls.remote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.remote)


class RemoteCollectorHelpersTests(RemoteModuleMixin, unittest.TestCase):
    def test_latest_point_uses_exact_legend_column_not_last_core(self):
        rows = [{"legend": ["core0", "cpu", "core2"],
                 "data": [[1, 10, 40, 90], [2, 11, 45, 95], [3, 12, float("nan"), 99]]}]
        self.assertEqual(self.remote.latest_point(rows, "cpu"), 45)
        self.assertEqual(self.remote.latest_point(rows, "core2"), 99)
        self.assertIsNone(self.remote.latest_point(rows, "missing"))

    def test_latest_point_aggregates_temperatures_at_latest_valid_timestamp(self):
        rows = [{"legend": ["cpu0", "cpu1"],
                 "data": [[1, 40, 44], [2, 42, 50], [3, "bad", None]]}]
        self.assertEqual(self.remote.latest_point(rows, aggregate=True), 46)

    def test_latest_point_handles_true_nas_time_column_layout(self):
        rows = [{"legend": ["time", "available", "cached"],
                 "data": [[100, 20, 80], [101, 25, 75]]}]
        self.assertEqual(self.remote.latest_point(rows, "available"), 25)
        self.assertEqual(self.remote.latest_point(rows, "cached"), 75)

    def test_latest_point_supports_single_legend_legacy_shape(self):
        rows = [{"legend": "cpu", "data": [[1, "bad"], [2, 22.5], [3, float("nan")]]}]
        self.assertEqual(self.remote.latest_point(rows, "cpu"), 22.5)

    def test_data_rows_handles_wrapped_and_malformed_results(self):
        self.assertEqual(self.remote.rows({"data": [{"x": 1}]}), [{"x": 1}])
        self.assertEqual(self.remote.rows("bad"), [])

    def test_portal_normalization_selects_first_safe_http_url(self):
        self.assertEqual(self.remote.portal_url(
            {"web": "http://0.0.0.0:3000", "secure": {"url": "https://media.example:8443/app?x=1#frag"}}),
            "https://media.example:8443/app?x=1")
        self.assertIsNone(self.remote.portal_url("http://[::]:8080", "http://localhost:80"))
        self.assertIsNone(self.remote.portal_url("javascript:alert(1)", "http://user:pass@nas.local/"))

    def test_midclt_serializes_structured_arguments_as_json(self):
        completed = subprocess.CompletedProcess([], 0, stdout='[{}]', stderr="")
        with mock.patch.object(self.remote.subprocess, "run", return_value=completed) as call:
            self.remote.midclt("reporting.get_data", [{"name": "cpu"}], {"start": 1, "aggregate": False})
        self.assertEqual(call.call_args.args[0][-2], '[{"name":"cpu"}]')
        self.assertEqual(call.call_args.args[0][-1], '{"start":1,"aggregate":false}')

    def test_snapshot_skips_inventory_queries_and_keys(self):
        calls = []
        def fake_midclt(method, *args, **kwargs):
            calls.append(method)
            if method == "system.info":
                return {"hostname": "nas", "physmem": 1024}
            if method == "reporting.get_data":
                return []
            raise AssertionError("snapshot must not query inventory")
        with mock.patch.object(self.remote, "midclt", side_effect=fake_midclt), mock.patch("builtins.open", side_effect=OSError("no proc in test")):
            data = self.remote.collect(include_inventory=False)
        self.assertNotIn("apps", data)
        self.assertNotIn("pools", data)
        self.assertNotIn("alerts", data)
        self.assertNotIn("app.query", calls)
        self.assertNotIn("pool.query", calls)
        self.assertNotIn("alert.list", calls)

    def test_inventory_subquery_failure_omits_only_failed_key(self):
        def fake_midclt(method, *args, **kwargs):
            if method == "system.info":
                return {"hostname": "nas", "physmem": 1024}
            if method == "reporting.get_data":
                return []
            if method == "app.query":
                raise RuntimeError("app query unavailable")
            if method == "pool.query":
                return [{"name": "tank", "status": "ONLINE", "size": 10, "allocated": 2, "free": 8, "healthy": True}]
            if method == "alert.list":
                return []
            raise AssertionError(method)
        with mock.patch.object(self.remote, "midclt", side_effect=fake_midclt), mock.patch("builtins.open", side_effect=OSError("no proc in test")):
            data = self.remote.collect(include_inventory=True)
        self.assertNotIn("apps", data)
        self.assertIn("pools", data)
        self.assertIn("alerts", data)
        self.assertTrue(any(error.startswith("apps:") for error in data["errors"]))

    def test_total_system_and_metric_failure_is_not_empty_success(self):
        def fail(*args, **kwargs):
            raise RuntimeError("query failed")
        with mock.patch.object(self.remote, "midclt", side_effect=fail), mock.patch("builtins.open", side_effect=OSError("no proc")):
            with self.assertRaisesRegex(RuntimeError, "system and metrics collection failed"):
                self.remote.collect()

    def test_collector_projects_fields_without_exposing_raw_config(self):
        app = {"name": "jellyfin", "state": "RUNNING", "version": "1", "config": {"secret": "x"}, "env": {"TOKEN": "y"},
               "portals": {"local": "http://0.0.0.0:8096", "web": "https://media.example:8096/"}}
        responses = {
            "system.info": {"hostname": "nas", "version": "25.10", "uptime_seconds": 9, "physmem": 1000},
            "app.query": [app], "pool.query": [], "alert.list": [],
        }
        def fake_midclt(method, *args, **kwargs):
            if method == "reporting.get_data":
                return []
            return responses[method]
        with mock.patch.object(self.remote, "midclt", side_effect=fake_midclt), mock.patch("builtins.open", side_effect=OSError("no proc in test")):
            data = self.remote.collect(include_inventory=True)
        self.assertEqual(data["apps"], [{"name": "jellyfin", "state": "RUNNING", "version": "1", "update_available": None, "portal": "https://media.example:8096/"}])
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("TOKEN", json.dumps(data))
        self.assertIsNone(data["metrics"]["memory_percent"])


class RemoteActionTests(RemoteModuleMixin, unittest.TestCase):
    def run_remote_action(self, operation, job_results, timeout=110, monotonic_values=None):
        remote = self.remote
        calls = []
        def fake_midclt(method, *args, **kwargs):
            calls.append((method, args))
            if method == "app.query":
                return [{"name": "jellyfin", "state": "RUNNING"}]
            return len([call for call in calls if call[0] in ("app.start", "app.stop")])
        output = __import__("io").StringIO()
        clock_patch = mock.patch.object(remote.time, "monotonic", side_effect=monotonic_values) if monotonic_values is not None else contextlib.nullcontext()
        with (mock.patch.object(remote, "REQUEST", {"op": operation, "app": "jellyfin", "timeout": timeout}),
              mock.patch.object(remote, "midclt", side_effect=fake_midclt),
              mock.patch.object(remote, "wait_job", side_effect=job_results),
              mock.patch("sys.stdout", output), clock_patch):
            remote.main()
        return calls, json.loads(output.getvalue())

    def test_restart_stops_then_starts_after_success(self):
        calls, output = self.run_remote_action("restart", [
            {"job_id": 10, "state": "SUCCESS", "error": ""},
            {"job_id": 11, "state": "SUCCESS", "error": ""},
        ])
        self.assertEqual([call[0] for call in calls], ["app.query", "app.stop", "app.start"])
        self.assertTrue(output["ok"])
        self.assertEqual(output["data"]["phase"], "start")

    def test_restart_does_not_start_when_stop_fails(self):
        calls, output = self.run_remote_action("restart", [{"job_id": 10, "state": "FAILED", "error": "stop failed"}])
        self.assertEqual([call[0] for call in calls], ["app.query", "app.stop"])
        self.assertFalse(output["ok"])
        self.assertEqual(output["data"]["phase"], "stop")

    def test_restart_does_not_start_when_shared_deadline_is_exhausted(self):
        calls, output = self.run_remote_action("restart", [{"job_id": 10, "state": "SUCCESS", "error": ""}],
                                               timeout=110, monotonic_values=[10, 109, 110, 111, 119])
        self.assertEqual([call[0] for call in calls], ["app.query", "app.stop"])
        self.assertFalse(output["ok"])
        self.assertEqual(output["data"]["phase"], "stop")
        self.assertEqual(output["data"]["job"]["job_id"], 10)
        self.assertIn("app may remain stopped", output["error"])

    def test_unexpected_stop_poll_error_retains_id_and_never_starts(self):
        calls, output = self.run_remote_action("restart", [ValueError("malformed job response")])
        self.assertEqual([call[0] for call in calls], ["app.query", "app.stop"])
        self.assertTrue(output["unknown"])
        self.assertEqual(output["data"]["job"]["job_id"], 1)
        self.assertEqual(output["data"]["job"]["state"], "UNKNOWN")
        self.assertEqual(output["data"]["phase"], "stop")

    def test_unexpected_start_poll_error_retains_both_restart_ids(self):
        calls, output = self.run_remote_action("restart", [
            {"job_id": 1, "state": "SUCCESS", "error": ""},
            ValueError("malformed job response"),
        ])
        self.assertEqual([call[0] for call in calls], ["app.query", "app.stop", "app.start"])
        self.assertTrue(output["unknown"])
        self.assertEqual(output["data"]["job"]["job_id"], 2)
        self.assertEqual(output["data"]["stop_job"]["job_id"], 1)
        self.assertEqual(output["data"]["phase"], "start")

    def test_remote_job_poll_uses_returned_job_id_and_deadline(self):
        remote = self.remote
        with (mock.patch.object(remote, "midclt", return_value=[{"id": 31, "state": "SUCCESS"}]) as midclt,
              mock.patch.object(remote.time, "monotonic", side_effect=[50, 51, 52, 53])):
            status = remote.wait_job(31, 55)
        self.assertEqual(status["state"], "SUCCESS")
        self.assertEqual(midclt.call_args.args[0], "core.get_jobs")

    def test_reconcile_is_read_only_and_preserves_restart_phase(self):
        remote = self.remote
        output = __import__("io").StringIO()
        pending = {"op": "restart", "phase": "stop", "job_id": 31, "stop_job_id": 31}
        with (mock.patch.object(remote, "REQUEST", {"op": "reconcile", "pending": pending}),
              mock.patch.object(remote, "wait_job", return_value={"job_id": 31, "state": "SUCCESS", "error": ""}),
              mock.patch.object(remote, "midclt") as midclt,
              mock.patch("sys.stdout", output)):
            remote.main()
        response = json.loads(output.getvalue())
        self.assertEqual(response["data"]["phase"], "stop")
        self.assertEqual(response["data"]["job"]["state"], "SUCCESS")
        midclt.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
