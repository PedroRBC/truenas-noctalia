"""Mock native widget freshness indicators without desktop or network access."""
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
local tree, tooltip, watch
local initial = {connected=true, stale=false, data={}}
noctalia = {
  getConfig=function() return nil end,
  tr=function(key) return key end,
  state={get=function() return initial end, watch=function(_, cb) watch=cb end},
}
local F = {running=function(apps) return #apps end, percent=function() return "—" end}
local require=function() return F end
ui = setmetatable({}, {__index=function(_, kind)
  return function(props, children) return {kind=kind, props=props, children=children or {}} end
end})
barWidget = {isVertical=function() return false end,
  render=function(value) tree=value end, setTooltip=function(value) tooltip=value end}
'''


@unittest.skipUnless(shutil.which("lua"), "optional Lua behavior harness unavailable")
class WidgetTests(unittest.TestCase):
    def run_case(self, case):
        source = (ROOT / "truenas/widget.luau").read_text().replace("request += 1", "request = request + 1")
        result = subprocess.run(["lua", "-"], input=HARNESS + source + "\n" + case,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_inventory_shows_unavailable_not_zero_apps(self):
        self.run_case('''
assert(tree.children[2].props.text == "—")
assert(tree.children[3].props.name == "alert-circle")
assert(tooltip:find("status.section_unavailable", 1, true))
''')

    def test_cached_inventory_warns_until_every_section_is_current(self):
        self.run_case('''
initial.data.apps = {{name="test"}}
initial.inventory_sections = {
  apps={available=true, stale=false}, pools={available=true, stale=true},
  alerts={available=true, stale=false}}
watch(initial)
assert(tree.children[2].props.text == "1/1" and #tree.children == 3)
assert(tooltip:find("status.section_stale", 1, true))
initial.inventory_sections.pools.stale = false
watch(initial)
assert(#tree.children == 2)
assert(not tooltip:find("status.section_stale", 1, true))
''')


if __name__ == "__main__":
    unittest.main()
