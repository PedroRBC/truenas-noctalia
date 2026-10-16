"""Mock service behavior in Lua; Noctalia separately validates real Luau loading.

The service's sole Luau-only syntax is +=. Lowering it here exercises coordinator
logic without pretending stock Lua is a Luau compiler. No SSH or desktop IPC runs.
"""
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
local clock = 1000000
local settings, values, watches, queue, decoded = {}, {}, {}, {}, {}
math.clamp = function(n, lo, hi) return math.max(lo, math.min(n, hi)) end
local function copy(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for k, v in pairs(value) do result[k] = copy(v) end
  return result
end
noctalia = {
  getConfig = function(key) return settings[key] end,
  nowMs = function() return clock end,
  pluginDir = function() return "/mock/plugin" end,
  pluginDataDir = function() return "/mock/data" end,
  setUpdateInterval = function() end,
  notify = function() end, notifyError = function() end,
  json = {decode = function(key)
    if key == "malformed" then error("invalid JSON") end
    return decoded[key]
  end},
  state = {
    set = function(key, value)
      values[key] = copy(value)
      if watches[key] then watches[key](copy(value)) end
    end,
    watch = function(key, callback) watches[key] = callback end,
  },
  runAsync = function(args, callback, timeout)
    table.insert(queue, {args=args, callback=callback, timeout=timeout})
    return true
  end,
}
local function finish(response)
  assert(#queue == 1, "expected one in-flight subprocess")
  local item = table.remove(queue, 1)
  decoded.response = response
  item.callback({stdout="response", stderr="", exitCode=response.ok and 0 or 1})
end
local function currentSnapshot() return values.truenas_snapshot end
local function send(action, confirmed)
  noctalia.state.set("truenas_request", {action=action, app="jellyfin",
    requestId=tostring(clock), confirmed=confirmed})
end
local function inventory()
  finish({ok=true, data={apps={{name="jellyfin", state="RUNNING"}},
    metrics={network_interface="br1", network_timestamp=1000,
      network_rx_bytes=100, network_tx_bytes=200}}})
end
'''


@unittest.skipUnless(shutil.which("lua"), "optional Lua behavior harness unavailable")
class ServiceTests(unittest.TestCase):
    def run_case(self, case):
        source = (ROOT / "truenas/service.luau").read_text()
        source = re.sub(r"([\w.]+) \+= ([^\n]+)", r"\1 = \1 + \2", source)
        result = subprocess.run(["lua", "-"], input=HARNESS + "\n" + source + "\n" + case,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readonly_and_confirmation_prevent_actions(self):
        self.run_case('''
inventory()
assert(currentSnapshot().inventory_fresh)
settings.read_only = true
send("stop", true)
assert(#queue == 0 and not currentSnapshot().busy)
settings.read_only = false
clock = clock + 1
send("stop", false)
assert(#queue == 0 and values.truenas_result.ok == false)
''')

    def test_unknown_outcome_locks_then_failed_terminal_reconcile_unlocks(self):
        self.run_case('''
inventory()
send("restart", true)
assert(currentSnapshot().busy)
finish({ok=false, unknown=true, error="timeout", pending={job_id=42}})
assert(currentSnapshot().pending and #queue == 1)
finish({ok=false, unknown=false, error="job failed", data={reconciled=true}})
assert(currentSnapshot().pending == nil and currentSnapshot().busy == false)
assert(currentSnapshot().error == "job failed")
assert(values.truenas_result.message == "job failed" and values.truenas_result.unknown == false)
''')

    def test_successful_reconciliation_replaces_unknown_feedback(self):
        self.run_case('''
inventory()
send("stop", true)
finish({ok=false, unknown=true, error="timeout", pending={job_id=42}})
finish({ok=true, unknown=false, pending=false})
assert(currentSnapshot().pending == nil and currentSnapshot().error == "")
assert(values.truenas_result.ok and not values.truenas_result.unknown)
assert(values.truenas_result.message:find("resolved"))
''')

    def test_old_endpoint_response_cannot_replace_new_endpoint(self):
        self.run_case('''
settings.ssh_host = "other.example"
onConfigChanged()
finish({ok=true, data={server={hostname="old-server"}}})
assert(currentSnapshot().data.server == nil and currentSnapshot().connected == false)
update()
assert(#queue == 1)
finish({ok=true, data={server={hostname="new-server"}}})
assert(currentSnapshot().data.server.hostname == "new-server")
''')

    def test_malformed_json_is_reported_not_stuck(self):
        self.run_case('''
local item = table.remove(queue, 1)
item.callback({stdout="malformed", stderr="bad data"})
assert(not currentSnapshot().refreshing and not currentSnapshot().connected)
assert(currentSnapshot().error:find("no valid response"))
''')

    def test_metrics_merge_inventory_and_network_counter_reset(self):
        self.run_case('''
inventory()
clock = clock + 11000
update()
finish({ok=true, data={metrics={network_interface="br1", network_timestamp=1011,
  network_rx_bytes=210, network_tx_bytes=420}}})
assert(#currentSnapshot().data.apps == 1 and currentSnapshot().rx_rate == 10 and currentSnapshot().tx_rate == 20)
clock = clock + 11000
update()
finish({ok=true, data={metrics={network_interface="br1", network_timestamp=1022,
  network_rx_bytes=1, network_tx_bytes=2}}})
assert(currentSnapshot().rx_rate == nil and currentSnapshot().tx_rate == nil)
''')

    def test_section_failures_persist_across_successful_metrics_refresh(self):
        self.run_case('''
finish({ok=true, data={apps={}, pools={{name="tank"}}, alerts={}, errors={}}})
local first = currentSnapshot().inventory_sections.pools.updated_at
clock = clock + 1000
send("refresh")
finish({ok=true, data={apps={}, errors={"pools: unavailable", "alerts: unavailable"}}})
assert(currentSnapshot().inventory_fresh)
assert(currentSnapshot().inventory_sections.pools.stale)
assert(currentSnapshot().inventory_sections.alerts.stale)
clock = clock + 11000
update()
assert(queue[1].args[#queue[1].args] == "snapshot")
finish({ok=true, data={metrics={cpu_percent=5}, errors={}, truncated=false}})
local s = currentSnapshot()
assert(s.inventory_sections.pools.error == "pools: unavailable")
assert(s.inventory_sections.pools.updated_at == first and #s.data.pools == 1)
assert(s.inventory_sections.alerts.available and s.inventory_sections.alerts.stale)
clock = clock + 1000
send("refresh")
finish({ok=true, data={apps={}, pools={}, alerts={}, errors={}}})
assert(not currentSnapshot().inventory_sections.alerts.stale)
assert(currentSnapshot().inventory_sections.alerts.error == nil)
''')

    def test_total_inventory_failure_survives_metrics_recovery(self):
        self.run_case('''
finish({ok=true, data={apps={}, pools={}, alerts={}, errors={}}})
clock = clock + 1000
send("refresh")
finish({ok=false, error="collection failed"})
clock = clock + 11000
update()
assert(queue[1].args[#queue[1].args] == "snapshot")
finish({ok=true, data={metrics={cpu_percent=5}, errors={}}})
assert(currentSnapshot().connected and not currentSnapshot().inventory_fresh)
assert(currentSnapshot().inventory_sections.alerts.error == "collection failed")
''')

    def test_never_loaded_sections_are_unavailable_not_empty(self):
        self.run_case('''
finish({ok=true, data={apps={}, errors={"pools: failed", "alerts: failed"}}})
assert(currentSnapshot().inventory_fresh)
assert(not currentSnapshot().inventory_sections.pools.available)
assert(not currentSnapshot().inventory_sections.alerts.available)
assert(currentSnapshot().data.alerts == nil)
''')

    def test_failed_app_inventory_immediately_disables_mutations(self):
        self.run_case('''
inventory()
clock = clock + 1000
send("refresh")
finish({ok=true, data={pools={}, alerts={}, errors={"apps: failed"}}})
assert(not currentSnapshot().inventory_fresh and #currentSnapshot().data.apps == 1)
clock = clock + 1000
send("stop", true)
assert(#queue == 0 and not currentSnapshot().busy)
''')

    def test_expired_inventory_rejects_mutation(self):
        self.run_case('''
inventory()
clock = clock + 121000
update()
finish({ok=true, data={metrics={cpu_percent=5}}})
assert(not currentSnapshot().inventory_fresh)
send("restart", true)
assert(#queue == 0 and values.truenas_result.ok == false)
''')


if __name__ == "__main__":
    unittest.main()
