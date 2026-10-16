"""Packaging/contract checks; live Luau compilation is tested by Noctalia itself."""
import json
import pathlib
import re
import shutil
import subprocess
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "truenas"


class PluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = tomllib.loads((PLUGIN / "plugin.toml").read_text())
        cls.translations = json.loads((PLUGIN / "translations/en.json").read_text())

    def translated(self, key):
        value = self.translations
        for part in key.split("."):
            value = value[part]
        self.assertIsInstance(value, str)
        self.assertTrue(value)

    def test_native_api_and_entry_files(self):
        self.assertEqual(self.manifest["id"], "PedroRBC/truenas")
        self.assertGreaterEqual(self.manifest["plugin_api"], 24)
        for kind in ("widget", "panel", "service"):
            for entry in self.manifest[kind]:
                path = PLUGIN / entry["entry"]
                self.assertTrue(path.is_file(), str(path))
                self.assertEqual(path.suffix, ".luau")

    def test_settings_types_ranges_and_translations(self):
        groups = [self.manifest.get("setting", [])]
        for kind in ("widget", "panel", "service"):
            groups.extend(entry.get("setting", []) for entry in self.manifest[kind])
        for settings in groups:
            keys = set()
            for setting in settings:
                with self.subTest(key=setting["key"]):
                    self.assertNotIn(setting["key"], keys)
                    keys.add(setting["key"])
                    for field in ("label_key", "description_key"):
                        self.translated(setting[field])
                    default = setting["default"]
                    expected = {"bool": bool, "int": int, "string": str, "select": str}[setting["type"]]
                    self.assertIs(type(default), expected)
                    if "min" in setting:
                        self.assertGreaterEqual(default, setting["min"])
                        self.assertLessEqual(default, setting["max"])
                    if setting["type"] == "select":
                        self.assertIn(default, [option["value"] for option in setting["options"]])
                        for option in setting["options"]:
                            self.translated(option["label_key"])

    def test_literal_ui_translation_keys_exist(self):
        for path in PLUGIN.glob("*.luau"):
            for key in re.findall(r'(?:tr|noctalia\.tr)\("([\w.]+)"', path.read_text()):
                # A trailing dot denotes a deliberately dynamic key prefix.
                if not key.endswith("."):
                    with self.subTest(file=path.name, key=key):
                        self.translated(key)

    def test_subprocesses_use_direct_argument_arrays(self):
        source = (PLUGIN / "service.luau").read_text()
        self.assertIn("noctalia.runAsync(args,", source)
        self.assertIn('"python3", "-I"', source)
        self.assertNotIn("shellQuote", source)
        self.assertNotIn("app.redeploy", source)
        self.assertIn('command.confirmed ~= true', source)
        self.assertIn('cfg("read_only", false) == true', source)

    @unittest.skipUnless(pathlib.Path("/usr/share/noctalia/assets/fonts/tabler.json").is_file(),
                         "installed Noctalia glyph catalog unavailable")
    def test_literal_glyphs_exist(self):
        catalog = json.loads(pathlib.Path("/usr/share/noctalia/assets/fonts/tabler.json").read_text())
        for path in PLUGIN.glob("*.luau"):
            for glyph in re.findall(r'(?:name|glyph) = "([a-z0-9-]+)"', path.read_text()):
                with self.subTest(file=path.name, glyph=glyph):
                    self.assertIn(glyph, catalog)

    def test_remote_script_and_bridge_packaged(self):
        self.assertTrue((PLUGIN / "scripts/truenas_bridge.py").is_file())
        self.assertTrue((PLUGIN / "scripts/remote.py").is_file())

    @unittest.skipUnless(shutil.which("noctalia"), "Noctalia is not installed")
    def test_native_settings_linter(self):
        result = subprocess.run(["noctalia", "plugins", "lint", str(PLUGIN)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
