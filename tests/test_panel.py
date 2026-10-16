"""Optional mocked UI callbacks; actual layout/compilation is checked in Noctalia."""
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
local settings = {favorites="jellyfin", hidden_apps="hidden"}
local watches, sent, tree = {}, {}, nil
local initial = {connected=true, stale=false, inventory_fresh=true,
  updated_at=100, target="root@example", data={apps={
    {name="alpha", state="STOPPED"}, {name="jellyfin", state="RUNNING"},
    {name="hidden", state="RUNNING"}}}}
math.clamp = function(n, lo, hi) return math.max(lo, math.min(n, hi)) end
noctalia = {
  getConfig=function(key) return settings[key] end,
  tr=function(key) return key end,
  nowMs=function() return 100000 end,
  formatTime=function() return "12:00:00" end,
  state={get=function() return initial end,
    set=function(key, value) sent[key]=value end,
    watch=function(key, cb) watches[key]=cb end},
  runAsync=function() error("no external commands in tests") end,
}
ui = setmetatable({}, {__index=function(_, kind)
  return function(props, children) return {kind=kind, props=props, children=children or {}} end
end})
panel={render=function(value) tree=value end, close=function() end,
  setWantsSecondTicks=function() end}
local function collect(predicate)
  local result={}
  local function visit(node)
    if predicate(node) then table.insert(result,node) end
    for _, child in ipairs(node.children or {}) do visit(child) end
  end
  visit(tree)
  return result
end
local function keyed(key)
  return collect(function(node) return node.props.key==key end)[1]
end
local function appRows()
  return collect(function(node) return (node.props.key or ""):match("^app%-") and node.kind=="row" end)
end
'''


def plain_format():
    source = (ROOT / "truenas/format.luau").read_text()
    source = re.sub(r"(?m)^(function Format\.\w+)\((\w+): any\):[^\n]+$", r"\1(\2)", source)
    for old, new in (("number /= 1024", "number = number / 1024"),
                     ("index += 1", "index = index + 1"),
                     ("count += 1", "count = count + 1")):
        source = source.replace(old, new)
    return "local function loadFormat()\n" + source + "\nend\nlocal require=function() return loadFormat() end\n"


@unittest.skipUnless(shutil.which("lua"), "optional Lua behavior harness unavailable")
class PanelTests(unittest.TestCase):
    def run_case(self, case):
        source = (ROOT / "truenas/panel.luau").read_text()
        source = source.replace("sequence += 1", "sequence = sequence + 1")
        source = source.replace("text ..=", "text = text ..")
        result = subprocess.run(["lua", "-"], input=HARNESS + plain_format() + source + "\n" + case,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_favorites_sort_hidden_names_search_and_filter(self):
        self.run_case('''
keyed("tab-apps").props.onClick()
assert(#appRows()==2 and appRows()[1].props.key=="app-jellyfin")
keyed("app-search").props.onChange("ALP")
update()
assert(#appRows()==1 and appRows()[1].props.key=="app-alpha")
keyed("app-search").props.onChange("")
keyed("app-filter").props.onChange("1")
update()
assert(#appRows()==1 and appRows()[1].props.key=="app-jellyfin")
''')

    def test_applications_containers_explicitly_stretch_horizontally(self):
        self.run_case('''
keyed("tab-apps").props.onClick()
assert(tree.props.align=="stretch")
local content=tree.children[#tree.children-1]
assert(content.kind=="column" and content.props.align=="stretch")
assert(keyed("apps-scroll").props.align=="stretch")
assert(keyed("app-search").props.flexGrow==1)
assert(appRows()[1].props.align=="center")
''')

    def test_formatting_preserves_small_nonzero_values_and_missing_data(self):
        self.run_case('''
assert(F.percent(0)=="0%" and F.percent(0.175)=="0.2%" and F.percent(0.01)=="<0.1%")
assert(F.percent(nil)=="—" and F.bytes(nil)=="—" and F.rate(nil)=="—")
assert(F.bytes(1048576)=="1.0 MiB" and F.bytes(102400)=="100 KiB")
assert(F.number(math.huge)==nil and F.number(0/0)==nil)
assert(F.url("https://example.test:443/app") and not F.url("file:///etc/passwd"))
assert(not F.url("https://example.test/a b"))
''')

    def test_confirmation_precedes_mutation(self):
        self.run_case('''
keyed("tab-apps").props.onClick()
assert(keyed("jellyfin-stop").props.enabled)
keyed("jellyfin-stop").props.onClick()
assert(sent.truenas_request==nil)
local buttons=collect(function(n) return n.kind=="button" and n.props.text=="actions.stop" end)
assert(#buttons==1 and buttons[1].props.enabled)
buttons[1].props.onClick()
assert(sent.truenas_request.action=="stop" and sent.truenas_request.confirmed==true)
''')

    def test_readonly_and_refreshing_disable_app_controls(self):
        self.run_case('''
settings.read_only=true
keyed("tab-apps").props.onClick()
assert(not keyed("jellyfin-stop").props.enabled and not keyed("alpha-start").props.enabled)
settings.read_only=false
initial.refreshing=true
watches.truenas_snapshot(initial)
update()
assert(not keyed("jellyfin-stop").props.enabled)
''')

    def test_unavailable_inventory_is_not_reported_as_empty_healthy(self):
        self.run_case('''
for _, tab in ipairs({"apps", "storage", "alerts"}) do
  initial.data = {}
  initial.inventory_sections = {}
  watches.truenas_snapshot(initial)
  keyed("tab-" .. tab).props.onClick()
  assert(#collect(function(n) return n.props.text == "status.section_unavailable" end) > 0)
  assert(#collect(function(n) return n.props.text == "alerts.empty" or n.props.text == "storage.empty" or n.props.text == "apps.empty" end) == 0)
end
''')

    def test_cached_empty_alerts_are_distinguished_from_current_empty(self):
        self.run_case('''
initial.data.alerts = {}
initial.inventory_sections = {alerts={available=true, stale=true}}
watches.truenas_snapshot(initial)
keyed("tab-alerts").props.onClick()
assert(#collect(function(n) return n.props.text == "alerts.cached_empty" end) == 1)
assert(#collect(function(n) return (n.props.text or ""):find("status.section_stale", 1, true) end) > 0)
initial.inventory_sections.alerts.stale = false
watches.truenas_snapshot(initial)
update()
assert(#collect(function(n) return n.props.text == "alerts.empty" end) == 1)
''')

    def test_endpoint_change_cancels_confirmation(self):
        self.run_case('''
keyed("tab-apps").props.onClick()
keyed("jellyfin-stop").props.onClick()
initial.target="root@different"
watches.truenas_snapshot(initial)
update()
assert(keyed("jellyfin-stop")~=nil and sent.truenas_request==nil)
''')


if __name__ == "__main__":
    unittest.main()
