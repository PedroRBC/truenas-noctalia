from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import tomllib
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import install


BASE_SETTINGS = '''config_version = 14

[bar]
order = [ "default" ]

    [bar.default]
    center = [ "active_window" ]
    end = [
        "media",
        "mini-docker",
        "widget"
    ]
    start = [ "launcher", "clock" ]

[plugins]
enabled = [ "example/existing" ]

[widget.mini-docker]
type = "8bury/mini-docker:mini-docker"

[widget.clock]
format = "{:%H:%M}"
'''


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.toml"
        self.config = self.root / "config.toml"
        self.state = self.root / "installer-state"
        self.settings.write_text(BASE_SETTINGS, encoding="utf-8")
        self.config.write_text("[theme]\nmode = \"dark\"\n", encoding="utf-8")

    def run_cli(self, *args: str, no_ipc: bool = True) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        cli_args = [
            "--config", str(self.config),
            "--settings", str(self.settings),
            "--state-dir", str(self.state),
        ]
        if no_ipc:
            cli_args.append("--no-ipc")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = install.main([*cli_args, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_action_is_dry_run_and_does_not_change_files(self) -> None:
        original = self.settings.read_text(encoding="utf-8")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Mode: dry run", out)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original)
        self.assertFalse(self.state.exists())

    def test_apply_adds_only_alias_and_default_bar_entry(self) -> None:
        original = self.settings.read_text(encoding="utf-8")
        code, _, err = self.run_cli("--apply")
        self.assertEqual((code, err), (0, ""))
        installed_text = self.settings.read_text(encoding="utf-8")
        installed = tomllib.loads(installed_text)
        self.assertEqual(installed["widget"]["truenas"]["type"], install.WIDGET_TYPE)
        self.assertEqual(installed["bar"]["default"]["end"], ["media", "mini-docker", "widget", "truenas"])
        self.assertEqual(installed["plugins"]["enabled"], ["example/existing"])
        self.assertIn('        "mini-docker",\n        "widget"', installed_text)
        self.assertIn("[widget.clock]\nformat = \"{:%H:%M}\"", installed_text)
        self.assertTrue(any((self.state / "backups").glob("settings.toml.*.bak")))
        manifest = json.loads((self.state / "install.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["widget_alias_added"])
        self.assertTrue(manifest["bar_entry_added"])
        self.assertFalse(manifest["source_added"])
        self.assertFalse(manifest["plugin_enabled_by_installer"])
        self.assertEqual(installed_text.count("\"truenas\""), 1)
        self.assertEqual(original.count("\"truenas\""), 0)

    def test_apply_is_idempotent(self) -> None:
        self.assertEqual(self.run_cli("--apply")[0], 0)
        installed = self.settings.read_text(encoding="utf-8")
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), installed)
        manifest = json.loads((self.state / "install.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["widget_alias_added"])
        self.assertTrue(manifest["bar_entry_added"])
        self.assertEqual(self.run_cli("--uninstall", "--apply")[0], 0)
        self.assertNotIn("[widget.truenas]", self.settings.read_text(encoding="utf-8"))

    def test_uninstall_removes_only_owned_values_and_preserves_later_edits(self) -> None:
        self.assertEqual(self.run_cli("--apply")[0], 0)
        text = self.settings.read_text(encoding="utf-8")
        text = text.replace('format = "{:%H:%M}"', 'format = "{:%H:%M}"\ncustom_user_option = true')
        self.settings.write_text(text, encoding="utf-8")
        preview_before = self.settings.read_text(encoding="utf-8")
        code, preview, err = self.run_cli("--uninstall")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Mode: dry run", preview)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), preview_before)
        code, _, err = self.run_cli("--uninstall", "--apply")
        self.assertEqual((code, err), (0, ""))
        restored = self.settings.read_text(encoding="utf-8")
        parsed = tomllib.loads(restored)
        self.assertNotIn("truenas", parsed["widget"])
        self.assertNotIn("truenas", parsed["bar"]["default"]["end"])
        self.assertEqual(parsed["widget"]["clock"]["custom_user_option"], True)
        self.assertEqual(parsed["plugins"]["enabled"], ["example/existing"])
        self.assertFalse((self.state / "install.json").exists())

    def test_uninstall_fails_closed_when_alias_was_changed(self) -> None:
        self.assertEqual(self.run_cli("--apply")[0], 0)
        self.settings.write_text(
            self.settings.read_text(encoding="utf-8").replace(
                f'type = "{install.WIDGET_TYPE}"', 'type = "user/custom-widget"'
            ),
            encoding="utf-8",
        )
        code, _, err = self.run_cli("--uninstall")
        self.assertEqual(code, 2)
        self.assertIn("changed since install", err)
        self.assertIn("user/custom-widget", self.settings.read_text(encoding="utf-8"))

    def test_install_refuses_an_existing_alias_with_another_type(self) -> None:
        self.settings.write_text(BASE_SETTINGS + '[widget.truenas]\ntype = "other/plugin:status"\n', encoding="utf-8")
        before = self.settings.read_text(encoding="utf-8")
        code, _, err = self.run_cli("--apply")
        self.assertEqual(code, 2)
        self.assertIn("different type", err)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertFalse((self.state / "install.json").exists())

    def test_ipc_settings_changes_are_preserved_and_patch_uses_fresh_content(self) -> None:
        calls: list[list[str]] = []
        source_list = "official git https://example.invalid/official\ncommunity git https://example.invalid/community"

        def fake_ipc(command: list[str]) -> str:
            calls.append(command)
            if command == ["plugins", "source", "list"]:
                return source_list
            if command == ["plugins", "source", "add", install.SOURCE_NAME, "path", str(install.REPO_ROOT)]:
                text = self.settings.read_text(encoding="utf-8")
                text = text.replace(
                    'enabled = [ "example/existing" ]',
                    'enabled = [ "example/existing", "PedroRBC/truenas" ]',
                )
                text += '\n[plugin_settings."other/plugin"]\nuser_value = "preserved"\n'
                self.settings.write_text(text, encoding="utf-8")
                return ""
            if command == ["plugins", "list"]:
                return "PedroRBC/truenas [truenas-local] 1.0.0 disabled"
            if command == ["plugins", "enable", install.PLUGIN_ID]:
                text = self.settings.read_text(encoding="utf-8")
                text = text.replace(
                    'enabled = [ "example/existing", "PedroRBC/truenas" ]',
                    'enabled = [ "example/existing", "PedroRBC/truenas", "user/other" ]',
                )
                self.settings.write_text(text, encoding="utf-8")
                return ""
            if command == ["config-reload"]:
                return ""
            self.fail(f"Unexpected IPC command: {command}")

        with (
            mock.patch.object(install, "DEFAULT_SETTINGS", self.settings),
            mock.patch.object(install, "DEFAULT_CONFIG", self.config),
            mock.patch.object(install, "_ipc", side_effect=fake_ipc),
        ):
            code, _, err = self.run_cli("--apply", no_ipc=False)
        self.assertEqual((code, err), (0, ""))
        parsed = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(parsed["plugins"]["enabled"], ["example/existing", "PedroRBC/truenas", "user/other"])
        self.assertEqual(parsed["plugin_settings"]["other/plugin"]["user_value"], "preserved")
        self.assertEqual(parsed["widget"]["truenas"]["type"], install.WIDGET_TYPE)
        self.assertEqual(parsed["bar"]["default"]["end"][-1], "truenas")
        manifest = json.loads((self.state / "install.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["source_added"])
        self.assertTrue(manifest["plugin_enabled_by_installer"])
        self.assertLess(calls.index(["plugins", "source", "add", install.SOURCE_NAME, "path", str(install.REPO_ROOT)]), calls.index(["plugins", "enable", install.PLUGIN_ID]))

    def test_install_retries_once_when_settings_change_during_atomic_write(self) -> None:
        original_atomic_write = install._atomic_write
        attempts = 0

        def change_once_then_write(path: Path, original: str, updated: str, state_dir: Path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                concurrent = path.read_text(encoding="utf-8").replace(
                    'format = "{:%H:%M}"',
                    'format = "{:%H:%M}"\nconcurrent_user_setting = true',
                )
                path.write_text(concurrent, encoding="utf-8")
            return original_atomic_write(path, original, updated, state_dir)

        with mock.patch.object(install, "_atomic_write", side_effect=change_once_then_write):
            code, _, err = self.run_cli("--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(attempts, 2)
        parsed = tomllib.loads(self.settings.read_text(encoding="utf-8"))
        self.assertTrue(parsed["widget"]["clock"]["concurrent_user_setting"])
        self.assertEqual(parsed["widget"]["truenas"]["type"], install.WIDGET_TYPE)
        self.assertIn("truenas", parsed["bar"]["default"]["end"])

    def test_uninstall_removes_widget_after_ipc_persists_stale_config(self) -> None:
        self.assertEqual(self.run_cli("--apply")[0], 0)
        stale_installed_settings = self.settings.read_text(encoding="utf-8")
        manifest_path = self.state / "install.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "no_ipc": False,
            "source_added": True,
            "source_name": install.SOURCE_NAME,
            "source_location": str(install.REPO_ROOT),
            "plugin_enabled_by_installer": True,
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        calls: list[list[str]] = []

        def fake_ipc(command: list[str]) -> str:
            calls.append(command)
            if command == ["plugins", "source", "list"]:
                return f"{install.SOURCE_NAME} path {install.REPO_ROOT}"
            if command in (["plugins", "disable", install.PLUGIN_ID], ["plugins", "source", "remove", install.SOURCE_NAME]):
                self.settings.write_text(stale_installed_settings, encoding="utf-8")
                return ""
            if command == ["config-reload"]:
                parsed = tomllib.loads(self.settings.read_text(encoding="utf-8"))
                self.assertNotIn("truenas", parsed["widget"])
                self.assertNotIn("truenas", parsed["bar"]["default"]["end"])
                return ""
            self.fail(f"Unexpected IPC command: {command}")

        with (
            mock.patch.object(install, "DEFAULT_SETTINGS", self.settings),
            mock.patch.object(install, "DEFAULT_CONFIG", self.config),
            mock.patch.object(install, "_ipc", side_effect=fake_ipc),
        ):
            code, _, err = self.run_cli("--uninstall", "--apply", no_ipc=False)
        self.assertEqual((code, err), (0, ""))
        self.assertLess(calls.index(["plugins", "disable", install.PLUGIN_ID]), calls.index(["plugins", "source", "remove", install.SOURCE_NAME]))
        self.assertEqual(calls[-1], ["config-reload"])
        self.assertFalse(manifest_path.exists())

    def test_uninstall_refuses_to_remove_repointed_owned_source(self) -> None:
        self.assertEqual(self.run_cli("--apply")[0], 0)
        manifest_path = self.state / "install.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({
            "no_ipc": False,
            "source_added": True,
            "source_name": install.SOURCE_NAME,
            "source_location": str(install.REPO_ROOT),
            "plugin_enabled_by_installer": True,
        })
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = self.settings.read_text(encoding="utf-8")
        calls: list[list[str]] = []

        def fake_ipc(command: list[str]) -> str:
            calls.append(command)
            if command == ["plugins", "source", "list"]:
                return f"{install.SOURCE_NAME} path /user/repointed/source"
            self.fail(f"IPC mutation must not happen after source repoint: {command}")

        with (
            mock.patch.object(install, "DEFAULT_SETTINGS", self.settings),
            mock.patch.object(install, "DEFAULT_CONFIG", self.config),
            mock.patch.object(install, "_ipc", side_effect=fake_ipc),
        ):
            code, _, err = self.run_cli("--uninstall", "--apply", no_ipc=False)
        self.assertEqual(code, 2)
        self.assertIn("refusing to remove it", err)
        self.assertEqual(calls, [["plugins", "source", "list"]])
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertTrue(manifest_path.exists())

    def test_partial_source_add_failure_keeps_recoverable_ownership_record(self) -> None:
        def fake_ipc(command: list[str]) -> str:
            if command == ["plugins", "source", "list"]:
                return "official git https://example.invalid/official"
            if command == ["plugins", "source", "add", install.SOURCE_NAME, "path", str(install.REPO_ROOT)]:
                self.settings.write_text(self.settings.read_text(encoding="utf-8") + "\n# IPC changed settings\n", encoding="utf-8")
                return ""
            if command == ["plugins", "list"]:
                return "PedroRBC/truenas [truenas-local] 1.0.0 disabled"
            if command == ["plugins", "enable", install.PLUGIN_ID]:
                raise install.InstallError("simulated enable failure")
            if command in (["plugins", "disable", install.PLUGIN_ID], ["plugins", "source", "remove", install.SOURCE_NAME], ["config-reload"]):
                return ""
            self.fail(f"Unexpected IPC command: {command}")

        with (
            mock.patch.object(install, "DEFAULT_SETTINGS", self.settings),
            mock.patch.object(install, "DEFAULT_CONFIG", self.config),
            mock.patch.object(install, "_ipc", side_effect=fake_ipc),
        ):
            code, _, err = self.run_cli("--apply", no_ipc=False)
            self.assertEqual(code, 2)
            self.assertIn("simulated enable failure", err)
            manifest = json.loads((self.state / "install.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["source_added"])
            self.assertFalse(manifest["plugin_enabled_by_installer"])
            code, _, err = self.run_cli("--uninstall", "--apply", no_ipc=False)
        self.assertEqual((code, err), (0, ""))
        self.assertFalse((self.state / "install.json").exists())
        self.assertNotIn("[widget.truenas]", self.settings.read_text(encoding="utf-8"))

    def test_fixture_paths_cannot_trigger_live_ipc(self) -> None:
        code, _, err = self.run_cli("--apply", no_ipc=False)
        self.assertEqual(code, 2)
        self.assertIn("pass --no-ipc", err)
        self.assertFalse((self.state / "install.json").exists())

    def test_inline_array_patch_and_remove(self) -> None:
        text = '[bar.default]\nend = [ "clock", "mini-docker" ]\n'
        parsed = tomllib.loads(text)
        patched, added = install._append_bar_item(text, parsed)
        self.assertTrue(added)
        self.assertEqual(tomllib.loads(patched)["bar"]["default"]["end"], ["clock", "mini-docker", "truenas"])
        removed, did_remove = install._remove_bar_item(patched, tomllib.loads(patched))
        self.assertTrue(did_remove)
        self.assertEqual(tomllib.loads(removed)["bar"]["default"]["end"], ["clock", "mini-docker"])

    def test_table_locator_finds_target_after_other_tables(self) -> None:
        text = "[first]\na = 1\n[bar.default]\nend = []\n"
        start, end = install._find_bar_end(text)
        self.assertEqual(text[start:end], "[]")

    def test_missing_ownership_record_refuses_uninstall(self) -> None:
        code, _, err = self.run_cli("--uninstall")
        self.assertEqual(code, 2)
        self.assertIn("ownership record", err)


if __name__ == "__main__":
    unittest.main()
