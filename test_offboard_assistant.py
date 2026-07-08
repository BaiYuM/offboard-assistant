import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import offboard_assistant as oa
import ai_reviewer
import sync_bundle


class OffboardAssistantTests(unittest.TestCase):
    def test_mask_identifier_email(self):
        self.assertEqual(oa.mask_identifier("person@example.com"), "p***n@example.com")

    def test_mask_identifier_short(self):
        self.assertEqual(oa.mask_identifier("ab"), "a***")

    def test_stable_id_is_stable(self):
        self.assertEqual(oa.stable_id(["a", "b"]), oa.stable_id(["a", "b"]))

    def test_diff_marks_existing_modified_after_since_as_review(self):
        since = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
        baseline = [{"id": "1", "type": "browser_login_metadata", "created_at": "2025-01-01T00:00:00+00:00"}]
        current = [
            {
                "id": "1",
                "type": "browser_login_metadata",
                "created_at": "2025-01-01T00:00:00+00:00",
                "password_modified_at": "2026-07-07T00:00:00+00:00",
            }
        ]
        result = oa.diff_items(current, baseline, since)
        self.assertEqual(result[0]["cleanup_confidence"], "needs_review_modified_after_since")

    def test_diff_marks_new_after_since_as_high(self):
        since = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
        current = [{"id": "2", "type": "sensitive_file_location", "modified_at": "2026-07-07T00:00:00+00:00"}]
        result = oa.diff_items(current, [], since)
        self.assertEqual(result[0]["cleanup_confidence"], "high_new_after_since")

    def test_install_monitor_detects_new_app_and_path(self):
        previous = {
            "apps": [],
            "environment": [],
            "paths": [],
        }
        current = {
            "apps": [{"id": "app-1", "name": "Example", "install_location": "C:\\Example"}],
            "environment": [],
            "paths": [{"id": "path-1", "path": "C:\\Example", "modified_at": "2026-07-07T00:00:00+00:00"}],
        }
        signals = oa.diff_install_monitor_snapshots(previous, current)
        self.assertEqual(len(signals["new_apps"]), 1)
        self.assertEqual(len(signals["new_paths"]), 1)
        self.assertIn("C:\\Example", oa.install_paths_from_signals(signals))

    def test_install_monitor_detects_path_env_change(self):
        previous = {
            "apps": [],
            "environment": [{"id": "env-1", "name": "PATH", "path_entries": ["C:\\Windows"]}],
            "paths": [],
        }
        current = {
            "apps": [],
            "environment": [{"id": "env-1", "name": "PATH", "path_entries": ["C:\\Windows", "C:\\Tool"]}],
            "paths": [],
        }
        signals = oa.diff_install_monitor_snapshots(previous, current)
        self.assertEqual(signals["changed_path_like_environment"][0]["added_path_entries"], ["C:\\Tool"])

    def test_changed_path_only_is_not_reliable_install_signal(self):
        signals = {
            "new_apps": [],
            "removed_apps": [],
            "new_environment_variables": [],
            "removed_environment_variables": [],
            "changed_path_like_environment": [],
            "new_paths": [],
            "changed_paths": [{"id": "path-1"}],
        }
        self.assertFalse(oa.signals_have_changes(signals))

    def test_encrypted_bundle_roundtrip_when_crypto_available(self):
        if not sync_bundle.crypto_available():
            self.skipTest("cryptography is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "baseline.json").write_text('{"items": []}', encoding="utf-8")
            bundle = root / "bundle.enc"
            sync_bundle.export_encrypted_bundle(source, bundle, "passphrase")
            imported = sync_bundle.import_encrypted_bundle(bundle, target, "passphrase")
            self.assertEqual(imported, ["baseline.json"])
            self.assertEqual((target / "baseline.json").read_text(encoding="utf-8"), '{"items": []}')

    def test_cleanup_action_for_environment_variable_includes_safe_command(self):
        action = oa.cleanup_action_for_item(
            {
                "id": "env-1",
                "type": "environment_variable",
                "scope": "user",
                "name": "OPENAI_API_KEY",
                "cleanup_confidence": "high_new_after_since",
            }
        )
        self.assertFalse(action["automatic"])
        self.assertEqual(action["risk"], "medium")
        self.assertIn("SetEnvironmentVariable", action["commands"][0])

    def test_cleanup_action_for_browser_login_is_manual(self):
        action = oa.cleanup_action_for_item(
            {
                "id": "login-1",
                "type": "browser_login_metadata",
                "browser": "Chrome",
                "origin": "https://company.example",
            }
        )
        self.assertFalse(action["automatic"])
        self.assertEqual(action["risk"], "high_if_wrong_account")
        self.assertTrue(action["manual_steps"])

    def test_migrate_legacy_state_copies_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / oa.APP_DIR
            target = root / "new" / oa.APP_DIR
            legacy.mkdir()
            target.mkdir(parents=True)
            (legacy / oa.BASELINE_FILE).write_text('{"baseline": true}', encoding="utf-8")
            original_cwd = Path.cwd()
            try:
                import os

                os.chdir(root)
                migrated = oa.migrate_legacy_state_if_needed(target)
            finally:
                os.chdir(original_cwd)
            self.assertEqual(migrated, [oa.BASELINE_FILE])
            self.assertEqual((target / oa.BASELINE_FILE).read_text(encoding="utf-8"), '{"baseline": true}')

    def test_detect_secret_references_masks_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
            path.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
            findings = oa.detect_secret_references(path)
            self.assertEqual(findings[0]["kind"], "OpenAI API key")
            self.assertFalse(findings[0]["value_recorded"])
            self.assertNotIn(secret, str(findings))

    def test_sensitive_scan_includes_secret_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".claude"
            config_dir.mkdir()
            path = config_dir / "config.json"
            path.write_text('{"api_key": "sk-ant-abcdefghijklmnopqrstuvwxyz123456"}', encoding="utf-8")
            items = oa.scan_sensitive_locations([root])
            kinds = {finding["kind"] for finding in items[0]["secret_findings"]}
            self.assertIn("Anthropic API key", kinds)

    def test_codex_tmp_plugin_path_is_recommended_cleanup(self):
        item = {
            "type": "sensitive_file_location",
            "path": r"C:\Users\Lenovo\.codex\.tmp\plugins\plugin-a\config.json",
            **oa.categorize_path(r"C:\Users\Lenovo\.codex\.tmp\plugins\plugin-a\config.json"),
        }
        self.assertEqual(item["category"], "codex_temp_plugin_cache")
        self.assertEqual(item["recommendation"], "recommend_cleanup")
        self.assertEqual(oa.recommended_cleanup_target(item), r"C:\Users\Lenovo\.codex\.tmp\plugins")

    def test_ai_review_payload_excludes_secret_values(self):
        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        items = [
            {
                "id": "1",
                "type": "sensitive_file_location",
                "path": r"C:\x\.env",
                "secret_findings": [
                    {
                        "kind": "OpenAI API key",
                        "masked": oa.mask_secret(secret),
                        "fingerprint": oa.secret_fingerprint(secret),
                        "value_recorded": False,
                    }
                ],
            }
        ]
        payload = oa.ai_review_payload_for_items(items)
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["items"][0]["secret_kinds"], ["OpenAI API key"])

    def test_ai_review_result_filters_unknown_ids(self):
        result = ai_reviewer.normalize_review_result(
            {
                "summary": "ok",
                "selected_ids": ["known", "unknown"],
                "decisions": [
                    {"id": "known", "action": "select", "risk": "low", "reason": "cache"},
                    {"id": "unknown", "action": "select", "risk": "low", "reason": "invented"},
                ],
                "warnings": ["check"],
            },
            {"known"},
        )
        self.assertEqual(result["selected_ids"], ["known"])
        self.assertEqual(len(result["decisions"]), 1)

    def test_extract_json_object_handles_fenced_json(self):
        data = ai_reviewer.extract_json_object('```json\n{"selected_ids": ["a"]}\n```')
        self.assertEqual(data["selected_ids"], ["a"])

    def test_chat_cleanup_action_has_account_modes(self):
        action = oa.cleanup_action_for_item(
            {
                "id": "chat-1",
                "type": "chat_data_location",
                "app": "WeChat",
                "path": r"C:\Users\Lenovo\Documents\WeChat Files",
            }
        )
        modes = {mode["mode"] for mode in action["chat_cleanup_modes"]}
        self.assertEqual(modes, {"personal_account", "company_account", "unknown"})
        self.assertFalse(action["automatic"])

    def test_infer_account_owner_hint_defaults_to_unknown(self):
        self.assertEqual(oa.infer_account_owner_hint({"path": r"C:\random\file.txt"}), "unknown")

    def test_infer_account_owner_hint_matches_company_saas(self):
        self.assertEqual(
            oa.infer_account_owner_hint({"origin": "https://github.com/acme/infra"}),
            "company_account",
        )

    def test_infer_account_owner_hint_matches_personal_tool_path(self):
        self.assertEqual(
            oa.infer_account_owner_hint({"path": r"C:\Users\me\.claude\settings.json"}),
            "personal_account",
        )

    def test_infer_account_owner_hint_company_domain_config_wins(self):
        cfg = {"company_email_domains": ["@mycorp.example.com"]}
        self.assertEqual(
            oa.infer_account_owner_hint(
                {"origin": "https://app.example.com/login?email=user@mycorp.example.com"},
                cfg,
            ),
            "company_account",
        )

    def test_categorize_path_includes_account_owner_hint(self):
        info = oa.categorize_path(r"C:\Users\me\.claude\settings.json")
        self.assertIn("account_owner_hint", info)
        self.assertEqual(info["account_owner_hint"], "personal_account")

    def test_describe_item_includes_account_owner_prefix(self):
        import importlib
        gui = importlib.import_module("offboard_gui")
        title, _detail, _when = gui.describe_item(
            {
                "id": "x",
                "type": "browser_login_metadata",
                "browser": "Chrome",
                "profile": "Default",
                "origin": "https://github.com/acme",
                "account_owner_hint": "company_account",
            }
        )
        self.assertTrue(title.startswith("[公司] "), title)

    def test_describe_item_fallback_does_not_dump_raw_item(self):
        import importlib
        gui = importlib.import_module("offboard_gui")
        _title, detail, _when = gui.describe_item({"id": "x", "type": "made_up_type", "sensitive": "value"})
        self.assertNotIn("sensitive", detail)
        self.assertIn("unsupported type", detail)

    def test_cleanup_action_chat_personal_account_has_backup_step(self):
        action = oa.cleanup_action_for_item(
            {
                "id": "chat-2",
                "type": "chat_data_location",
                "app": "WeChat",
                "path": r"C:\Users\me\Documents\WeChat Files",
                "account_owner_hint": "personal_account",
            }
        )
        joined = "\n".join(action["manual_steps"])
        self.assertIn("personal", joined.lower())
        self.assertIn("back up", joined.lower())
        self.assertEqual(action["account_owner_hint"], "personal_account")

    def test_cleanup_action_includes_account_owner_hint_for_browser_login(self):
        action = oa.cleanup_action_for_item(
            {
                "id": "b-1",
                "type": "browser_login_metadata",
                "browser": "Edge",
                "profile": "Default",
                "origin": "https://github.com/acme",
                "account_owner_hint": "company_account",
            }
        )
        self.assertEqual(action["account_owner_hint"], "company_account")

    def test_ai_review_payload_includes_handled_items_without_title(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            handled_path = state_dir / "handled-items.json"
            handled_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "x-1",
                                "type": "sensitive_file_location",
                                "title": r"C:\Users\me\private\leaked.json",
                                "origin": "https://github.com/acme/secret-repo",
                                "handled_at": "2026-07-01T00:00:00+00:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = oa.ai_review_payload_for_items(
                [{"id": "a", "type": "sensitive_file_location", "path": "/x/y"}],
                state_dir=state_dir,
            )
            handled = payload["user_feedback"]["handled_items"]
            self.assertEqual(len(handled), 1)
            self.assertEqual(handled[0]["id"], "x-1")
            self.assertEqual(handled[0]["type"], "sensitive_file_location")
            self.assertIn("handled_at", handled[0])
            # Title and origin must be dropped.
            self.assertNotIn("title", handled[0])
            self.assertNotIn("origin", handled[0])

    def test_ai_review_payload_silently_skips_missing_handled_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            payload = oa.ai_review_payload_for_items(
                [{"id": "a", "type": "x"}], state_dir=Path(tmp)
            )
            self.assertNotIn("user_feedback", payload)

    def test_normalize_review_result_drops_excluded_ids(self):
        result = ai_reviewer.normalize_review_result(
            {
                "selected_ids": ["a", "x"],
                "decisions": [
                    {"id": "a", "action": "select", "risk": "low", "reason": "ok"},
                    {"id": "x", "action": "select", "risk": "low", "reason": "ok"},
                ],
            },
            allowed_ids={"a", "x"},
            excluded_ids={"x"},
        )
        self.assertEqual(result["selected_ids"], ["a"])
        self.assertEqual([d["id"] for d in result["decisions"]], ["a"])

    def test_ai_prompt_includes_handled_items_section(self):
        prompt = ai_reviewer.build_user_prompt(
            {
                "items": [],
                "user_feedback": {
                    "handled_items": [
                        {"id": "x-1", "type": "t", "handled_at": "2026-07-01T00:00:00+00:00"}
                    ]
                },
            }
        )
        self.assertIn("Handled items", prompt)
        self.assertIn("do NOT re-select", prompt)
        self.assertIn("x-1", prompt)

    def test_jetbrains_time_to_iso_parses_millis(self):
        from offboard_assistant import _jetbrains_time_to_iso
        # 2024-01-01T00:00:00Z == 1704067200000 ms
        iso = _jetbrains_time_to_iso("1704067200000")
        self.assertTrue(iso.startswith("2024-01-01"), iso)

    def test_jetbrains_time_to_iso_handles_iso(self):
        from offboard_assistant import _jetbrains_time_to_iso
        iso = _jetbrains_time_to_iso("2024-01-01T00:00:00Z")
        self.assertTrue(iso.startswith("2024-01-01"), iso)

    def test_scan_recent_ide_projects_parses_jetbrains_xml(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ide_dir = root / "IntelliJIdea2024.1" / "options"
            ide_dir.mkdir(parents=True)
            (ide_dir / "recentProjects.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<application>
  <component name="RecentProjectsManager">
    <option name="recentPaths">
      <list>
        <option value="$USER_HOME$/work/acme" />
      </list>
    </option>
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/work/acme">
          <value>
            <RecentProjectMetaInfo>
              <option name="projectName" value="acme-backend" />
              <option name="lastOpened" value="1704067200000" />
            </RecentProjectMetaInfo>
          </value>
        </entry>
      </map>
    </option>
  </component>
</application>
""",
                encoding="utf-8",
            )
            items = oa.scan_recent_ide_projects(custom_roots=[root])
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["type"], "ide_recent_project")
            self.assertEqual(item["ide"], "IntelliJIdea2024.1")
            self.assertEqual(item["name"], "acme-backend")
            self.assertTrue(item["last_opened_at"].startswith("2024-01-01"), item["last_opened_at"])
            self.assertEqual(item["contents_recorded"], False)
            self.assertEqual(item["value_recorded"], False)

    def test_scan_recent_ide_projects_skips_missing_dir(self):
        # Custom root that doesn't exist: must return [] cleanly.
        import tempfile
        from pathlib import Path
        items = oa.scan_recent_ide_projects(custom_roots=[Path(tempfile.gettempdir()) / "nonexistent-ide-dir"])
        self.assertEqual(items, [])

    def test_scan_recent_ide_projects_skips_garbage_xml(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            ide_dir = Path(tmp) / "PyCharm2024.2" / "options"
            ide_dir.mkdir(parents=True)
            (ide_dir / "recentProjects.xml").write_text("<<<not xml>>>", encoding="utf-8")
            items = oa.scan_recent_ide_projects(custom_roots=[Path(tmp)])
            self.assertEqual(items, [])

    def test_scan_recent_ide_projects_does_not_touch_vscdb_or_workspace_settings(self):
        # Build a directory tree that contains the things we MUST NOT read
        # (state.vscdb, argv.json, workspaceStorage/.../workspace.json with
        # a sentry dsn). Verify those files are unchanged after the scan.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ide_dir = root / "GoLand2024.3" / "options"
            ide_dir.mkdir(parents=True)
            (ide_dir / "recentProjects.xml").write_text(
                """<?xml version="1.0"?>
<application><component name="RecentProjectsManager">
<option name="recentPaths"><list><option value="/proj" /></list></option>
<option name="additionalInfo"><map>
<entry key="/proj"><value>
<RecentProjectMetaInfo><option name="projectName" value="proj" />
<option name="lastOpened" value="1704067200000" /></RecentProjectMetaInfo>
</value></entry>
</map></option>
</component></application>
""",
                encoding="utf-8",
            )
            # Distraction files the scanner MUST NOT read.
            decoy_dir = root / "decoy"
            decoy_dir.mkdir()
            vscdb = decoy_dir / "state.vscdb"
            vscdb.write_text("SQLite-format-binary-blob-with-token-cache", encoding="utf-8")
            argv_json = decoy_dir / "argv.json"
            argv_json.write_text('["C:\\\\Users\\\\me\\\\sensitive-app\\\\Code.exe"]', encoding="utf-8")
            ws_dir = decoy_dir / "workspaceStorage" / "abc123"
            ws_dir.mkdir(parents=True)
            ws_json = ws_dir / "workspace.json"
            ws_json.write_text(
                '{"folder":"file:///c%3A/proj","settings":{"sentry.dsn":"https://key@sentry.io/123"}}',
                encoding="utf-8",
            )
            vscdb_mtime_before = vscdb.stat().st_mtime_ns
            argv_mtime_before = argv_json.stat().st_mtime_ns
            ws_mtime_before = ws_json.stat().st_mtime_ns

            oa.scan_recent_ide_projects(custom_roots=[root])

            # If any decoy file had been opened, mtime would change.
            self.assertEqual(vscdb.stat().st_mtime_ns, vscdb_mtime_before, "vscdb must not be touched")
            self.assertEqual(argv_json.stat().st_mtime_ns, argv_mtime_before, "argv.json must not be touched")
            self.assertEqual(ws_json.stat().st_mtime_ns, ws_mtime_before, "workspace.json must not be touched")

    def test_item_times_reads_last_opened_at(self):
        item = {"id": "x", "last_opened_at": "2026-07-01T00:00:00+00:00"}
        times = oa.item_times(item)
        self.assertEqual(len(times), 1)
        self.assertEqual(times[0].year, 2026)

    def test_scan_recent_ide_projects_handles_macos_application_support_path(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            # Simulate the macOS layout: ~/Library/Application Support/JetBrains/<IDE>/...
            root = Path(tmp) / "Library" / "Application Support" / "JetBrains"
            ide_dir = root / "IntelliJIdea2024.1" / "options"
            ide_dir.mkdir(parents=True)
            (ide_dir / "recentProjects.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<application>
  <component name="RecentProjectsManager">
    <option name="recentPaths">
      <map>
        <entry key="$USER_HOME$/macos-app">
          <value>
            <RecentProjectMetaInfo>
              <option name="projectName" value="macos-app"/>
              <option name="lastOpened" value="1704067200000"/>
            </RecentProjectMetaInfo>
          </value>
        </entry>
      </map>
    </option>
  </component>
</application>
""",
                encoding="utf-8",
            )
            items = oa.scan_recent_ide_projects(custom_roots=[root])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["ide"], "IntelliJIdea2024.1")
            self.assertEqual(items[0]["name"], "macos-app")
            self.assertEqual(items[0]["type"], "ide_recent_project")
            self.assertEqual(items[0]["path"], "$USER_HOME$/macos-app")

    def test_collect_snapshot_includes_ide_recent_items(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ide_dir = root / "IntelliJIdea2024.1" / "options"
            ide_dir.mkdir(parents=True)
            (ide_dir / "recentProjects.xml").write_text(
                """<?xml version="1.0"?>
<application><component name="RecentProjectsManager">
<option name="recentPaths"><list><option value="/work/acme" /></list></option>
<option name="additionalInfo"><map>
<entry key="/work/acme"><value>
<RecentProjectMetaInfo><option name="projectName" value="acme" />
<option name="lastOpened" value="1704067200000" /></RecentProjectMetaInfo>
</value></entry>
</map></option>
</component></application>
""",
                encoding="utf-8",
            )
            # Stub out the other scanners so the test only exercises the IDE
            # integration path. ``scan_browser_logins`` in particular touches
            # real Chromium databases and creates temp sqlite files whose
            # cleanup races with Windows file locking in CI.
            with mock.patch.object(oa, "scan_browser_logins", return_value=[]), \
                 mock.patch.object(oa, "scan_installed_apps_from_registry", return_value=[]):
                snapshot = oa.collect_snapshot(Path(tmp) / "state", roots=[root])
            ide_items = [it for it in snapshot["items"] if it.get("type") == "ide_recent_project"]
            self.assertGreaterEqual(len(ide_items), 1)
            self.assertEqual(snapshot["privacy"]["ide_recent_project_contents_recorded"], False)

    def test_local_config_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            config = oa.default_config()
            config["company_email_domains"] = ["@mycorp.example.com"]
            config["scan_roots"] = [str(Path(tmp) / "scan")]
            oa.save_local_config(state_dir, config)
            loaded = oa.load_local_config(state_dir)
            self.assertEqual(loaded["company_email_domains"], ["@mycorp.example.com"])
            self.assertEqual(loaded["scan_roots"], [str(Path(tmp) / "scan")])
            # Default keys should still be present.
            self.assertIn("schema_version", loaded)

    def test_local_config_handles_missing_file(self):
        import tempfile
        from pathlib import Path
        cfg = oa.load_local_config(Path(tempfile.gettempdir()) / "nonexistent-offboard-state")
        self.assertEqual(cfg["schema_version"], 1)
        self.assertEqual(cfg["company_email_domains"], [])

    def test_local_config_silently_drops_unknown_keys(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / oa.CONFIG_FILE).write_text(
                json.dumps({"schema_version": 1, "company_email_domains": ["@x"], "rogue_key": "value"}),
                encoding="utf-8",
            )
            cfg = oa.load_local_config(state_dir)
            self.assertEqual(cfg["company_email_domains"], ["@x"])
            self.assertNotIn("rogue_key", cfg)

    def test_wizard_done_roundtrip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            self.assertFalse(oa.is_wizard_done(state_dir))
            oa.mark_wizard_done(state_dir)
            self.assertTrue(oa.is_wizard_done(state_dir))

    def test_resolve_scan_roots_precedence(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cli_root = Path(tmp) / "cli"
            cli_root.mkdir()
            cfg_root = Path(tmp) / "config"
            cfg_root.mkdir()
            config = {"scan_roots": [str(cfg_root)]}
            # CLI + config: both end up in the result (additive), CLI first.
            roots = oa.resolve_scan_roots([str(cli_root)], config)
            resolved = [str(r) for r in roots]
            self.assertIn(str(cli_root.resolve()), resolved)
            self.assertIn(str(cfg_root.resolve()), resolved)
            self.assertLess(resolved.index(str(cli_root.resolve())), resolved.index(str(cfg_root.resolve())))
            # No CLI: config wins, no default.
            roots = oa.resolve_scan_roots([], config)
            self.assertEqual([str(r) for r in roots], [str(cfg_root.resolve())])
            # No config, no CLI: default roots.
            roots = oa.resolve_scan_roots([], {})
            self.assertGreater(len(roots), 0)

    def test_restore_quarantine_round_trip(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            source_dir = state_dir / "source"
            source_dir.mkdir()
            original = source_dir / "data.txt"
            original.write_text("payload", encoding="utf-8")
            bundle = state_dir / "quarantine" / "20260701-120000"
            bundle.mkdir(parents=True)
            # Simulate a previous quarantine: original was moved away, the
            # file now lives only at the destination inside the bundle.
            moved = bundle / "001-data.txt"
            shutil.move(str(original), str(moved))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "source": str(original),
                                "destination": str(moved),
                                "item_id": "x",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = oa.restore_quarantine_dir(bundle)
            self.assertEqual(result["restored"], [str(original)])
            self.assertEqual(result["skipped"], [])
            self.assertEqual(result["errors"], [])
            self.assertTrue(original.exists())
            self.assertFalse(moved.exists())

    def test_restore_quarantine_skips_existing_source(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            source_dir = state_dir / "source"
            source_dir.mkdir()
            original = source_dir / "data.txt"
            original.write_text("keep me", encoding="utf-8")
            bundle = state_dir / "quarantine" / "20260701-120000"
            bundle.mkdir(parents=True)
            moved = bundle / "001-data.txt"
            moved.write_text("old copy", encoding="utf-8")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "source": str(original),
                                "destination": str(moved),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = oa.restore_quarantine_dir(bundle)
            self.assertEqual(result["restored"], [])
            self.assertEqual(result["skipped"], [str(original)])
            # The original file is NOT overwritten.
            self.assertEqual(original.read_text(encoding="utf-8"), "keep me")

    def test_restore_quarantine_handles_missing_manifest(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "quarantine" / "no-manifest"
            bundle.mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                oa.restore_quarantine_dir(bundle)

    def test_purge_quarantine_removes_bundle(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "quarantine" / "20260701"
            bundle.mkdir(parents=True)
            (bundle / "file.txt").write_text("x", encoding="utf-8")
            result = oa.purge_quarantine_dir(bundle)
            self.assertTrue(result["purged"])
            self.assertFalse(bundle.exists())

    def test_list_quarantine_bundles_skips_bad_manifest(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            good = state_dir / "quarantine" / "20260701"
            good.mkdir(parents=True)
            (good / "manifest.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            bad = state_dir / "quarantine" / "20260702"
            bad.mkdir(parents=True)
            (bad / "manifest.json").write_text("not json", encoding="utf-8")
            bundles = oa.list_quarantine_bundles(state_dir)
            self.assertEqual(len(bundles), 2)
            ts_to_bundle = {b["ts"]: b for b in bundles}
            self.assertEqual(ts_to_bundle["20260701"]["items"], [])
            self.assertIn("manifest_unreadable", ts_to_bundle["20260702"]["errors"])

    def test_quarantine_index_records_and_lists_batches(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            bundle = state_dir / "quarantine" / "20260801-100000"
            bundle.mkdir(parents=True)
            source = state_dir / "src" / "f.txt"
            source.parent.mkdir(parents=True)
            source.write_text("hello", encoding="utf-8")
            moved = bundle / "001-f.txt"
            moved.write_text("hello", encoding="utf-8")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {"item_id": "x", "source": str(source), "destination": str(moved), "category": "codex_temp_plugin_cache", "moved_at": oa.utc_now()},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            oa._index_quarantine_batch(state_dir, bundle, manifest)

            # Index file must exist with the batch recorded.
            self.assertTrue((state_dir / oa.QUARANTINE_INDEX_FILE).exists())
            rows = oa.query_quarantine_index(state_dir)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["batch_id"], "20260801-100000")
            self.assertEqual(rows[0]["source_count"], 1)
            self.assertGreater(rows[0]["source_bytes"], 0)

            # items table populated too.
            conn = sqlite3.connect(str(state_dir / oa.QUARANTINE_INDEX_FILE))
            items = conn.execute("SELECT item_id, source, destination, category FROM items").fetchall()
            conn.close()
            self.assertEqual(items, [("x", str(source), str(moved), "codex_temp_plugin_cache")])

    def test_quarantine_index_silent_on_filesystem_error(self):
        """A bad state_dir path must not raise; just return []."""
        result = oa.query_quarantine_index(Path("Z:/does/not/exist/anywhere"))
        self.assertEqual(result, [])

    def test_restore_bumps_quarantine_index_restored_count(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            source_dir = state_dir / "src"
            source_dir.mkdir(parents=True)
            original = source_dir / "f.txt"  # intentionally not created
            bundle = state_dir / "quarantine" / "20260802-110000"
            bundle.mkdir(parents=True)
            moved = bundle / "001-f.txt"
            moved.write_text("payload", encoding="utf-8")
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {"item_id": "x", "source": str(original), "destination": str(moved)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            # Pre-seed the index with the batch (mirrors what the GUI writes
            # after a quarantine).
            oa._index_quarantine_batch(state_dir, bundle, json.loads((bundle / "manifest.json").read_text(encoding="utf-8")))

            result = oa.restore_quarantine_dir(bundle)
            self.assertEqual(len(result["restored"]), 1)
            rows = oa.query_quarantine_index(state_dir)
            self.assertEqual(rows[0]["restored_count"], 1)


class PortableModeTests(unittest.TestCase):
    def test_portable_state_dir_lives_next_to_executable(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            exe_parent = Path(tmp)
            exe_path = exe_parent / "OffboardAssistant.exe"
            exe_path.touch()  # doesn't have to be a real exe
            with mock.patch.object(oa.sys, "frozen", True, create=True), \
                 mock.patch.object(oa.sys, "executable", str(exe_path)):
                state = oa.portable_state_base()
                self.assertEqual(state, exe_parent / ".offboard_data")
                self.assertEqual(state.parent, exe_parent)

    def test_default_state_base_unaffected_when_not_frozen(self):
        # Sanity: portable path is only used when running as a frozen EXE.
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            prev = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                self.assertNotEqual(oa.default_state_base(), oa.portable_state_base())
            finally:
                if prev is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = prev

    def test_restore_quarantine_cli_subcommand(self):
        import tempfile
        from pathlib import Path
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            source = state_dir / "src"
            source.mkdir(parents=True)
            original = source / "f.txt"
            original.write_text("x", encoding="utf-8")
            bundle = state_dir / "quarantine" / "20260701-120000"
            bundle.mkdir(parents=True)
            moved = bundle / "001-f.txt"
            shutil.move(str(original), str(moved))
            (bundle / "manifest.json").write_text(
                json.dumps({"items": [{"source": str(original), "destination": str(moved)}]}),
                encoding="utf-8",
            )
            env = {"APPDATA": str(state_dir.parent), "PATH": __import__("os").environ.get("PATH", "")}
            r = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "offboard_assistant.py"),
                 "restore-quarantine", "--quarantine-dir", str(bundle)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("Restored: 1", r.stdout)

    def test_status_level_enum_palette(self):
        # Sanity: enum exposes 4 levels with the expected palette keys.
        import importlib
        gui = importlib.import_module("offboard_gui")
        names = [m.name for m in gui.StatusLevel]
        self.assertEqual(names, ["INFO", "WARN", "ERROR", "BUSY"])
        for level in gui.StatusLevel:
            self.assertTrue(level.indicator_fg.startswith("#"))
            self.assertTrue(level.text_bg.startswith("#"))

    def test_detail_panel_quarantine_routes_through_controller(self):
        # DetailPanel._quarantine_single should NOT do the move itself;
        # it must set controller.selected_ids and delegate to the
        # controller's batch path. This avoids two divergent implementations
        # of "move + write manifest + refresh".
        import importlib
        gui = importlib.import_module("offboard_gui")
        # Build a real Tk root just for object construction; withdraw it so
        # nothing flashes on screen during the test.
        root = gui.tk.Tk()
        try:
            root.withdraw()
            from unittest import mock
            controller = mock.MagicMock()
            controller.state_dir = root  # not actually used because we mock the call
            item = {
                "id": "x",
                "type": "sensitive_file_location",
                "path": "/tmp/some-tmp-dir",
                "recommendation": "recommend_cleanup",
            }
            # Patch the recommended_cleanup_target so the panel's own check passes.
            with mock.patch.object(gui.core, "recommended_cleanup_target", return_value="/tmp/some-tmp-dir"), \
                 mock.patch.object(gui.messagebox, "askyesno", return_value=True):
                panel = gui.DetailPanel(root, controller=controller)
                panel.show_item(item)
                panel._quarantine_single()
            controller.selected_ids = {"x"}
            controller.quarantine_selected_recommended.assert_called_once()
        finally:
            root.destroy()

    def test_detail_panel_mark_handled_routes_through_controller(self):
        import importlib
        gui = importlib.import_module("offboard_gui")
        root = gui.tk.Tk()
        try:
            root.withdraw()
            from unittest import mock
            controller = mock.MagicMock()
            item = {"id": "y", "type": "browser_login_metadata"}
            panel = gui.DetailPanel(root, controller=controller)
            panel.show_item(item)
            panel._mark_handled()
            controller.mark_selected_handled.assert_called_once()
        finally:
            root.destroy()

    def test_rules_default_yaml_loads(self):
        # Built-in defaults must come from rules/default.yaml when PyYAML
        # is available. Falls back to Python constants otherwise.
        rules = oa.load_rules()
        # 10 SaaS seed entries from the YAML.
        self.assertGreaterEqual(len(rules.saas_domains), 10)
        self.assertIn("github.com", rules.saas_domains)
        # 7 path rules from the YAML.
        self.assertGreaterEqual(len(rules.path_rules), 7)
        cats = {cat for cat, _, _, _ in rules.path_rules}
        self.assertIn("ssh_config", cats)
        self.assertIn("environment_file", cats)
        # 8 secret patterns.
        self.assertGreaterEqual(len(rules.secret_patterns), 8)
        kinds = {kind for kind, _ in rules.secret_patterns}
        self.assertIn("OpenAI API key", kinds)

    def test_rules_overrides_are_additive(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            rules_dir = state_dir / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "overrides.yaml").write_text(
                "saas_domains:\n"
                "  - mycorp.example.com\n"
                "  - jira.internal\n"
                "path_rules:\n"
                "  - category: ssh_config\n"
                "    needles:\n"
                "      - \"/.config/ssh/\"\n"
                "secret_patterns:\n"
                "  - kind: 'MyCorp token'\n"
                "    pattern: 'mc_[A-Za-z0-9]{20,}'\n",
                encoding="utf-8",
            )
            rules = oa.load_rules(state_dir)
            # SaaS additions: original + new.
            self.assertIn("github.com", rules.saas_domains)
            self.assertIn("mycorp.example.com", rules.saas_domains)
            self.assertIn("jira.internal", rules.saas_domains)
            # ssh_config category still present, needles now include both.
            ssh = next(r for r in rules.path_rules if r[0] == "ssh_config")
            self.assertIn("/.ssh/", ssh[1])
            self.assertIn("/.config/ssh/", ssh[1])
            # Custom secret pattern appended.
            kinds = {kind for kind, _ in rules.secret_patterns}
            self.assertIn("MyCorp token", kinds)
            self.assertTrue(rules.has_overrides)

    def test_rules_overrides_invalid_falls_back(self):
        # Garbage overrides.yaml must not crash; loader falls back to defaults.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "rules").mkdir(parents=True)
            (state_dir / "rules" / "overrides.yaml").write_text(
                ":\n  - this is not valid yaml: should fall back",  # still parses as nested
                encoding="utf-8",
            )
            rules = oa.load_rules(state_dir)
            self.assertGreaterEqual(len(rules.saas_domains), 10)
            self.assertFalse(rules.has_overrides if rules.overrides_path is None else False)

    def test_infer_account_owner_uses_yaml_saas(self):
        # Custom SaaS domain added via override should be detected as company.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "rules").mkdir(parents=True)
            (state_dir / "rules" / "overrides.yaml").write_text(
                "saas_domains:\n  - acme-corpus.internal\n",
                encoding="utf-8",
            )
            # Built-in github.com must still match.
            self.assertEqual(
                oa.infer_account_owner_hint(
                    {"origin": "https://github.com/x/y"}, state_dir=state_dir
                ),
                "company_account",
            )
            # Override-added domain also matches.
            self.assertEqual(
                oa.infer_account_owner_hint(
                    {"origin": "https://acme-corpus.internal/project"}, state_dir=state_dir
                ),
                "company_account",
            )

    def test_categorize_path_uses_yaml_rules(self):
        # Override path needles should extend the built-in rule.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "rules").mkdir(parents=True)
            (state_dir / "rules" / "overrides.yaml").write_text(
                "path_rules:\n"
                "  - category: ai_tool_config\n"
                "    needles:\n"
                "      - \"/.my-tool/\"\n",
                encoding="utf-8",
            )
            # /Users/me/.my-tool/settings.json should hit the merged rule.
            info = oa.categorize_path(r"C:\Users\me\.my-tool\settings.json", state_dir=state_dir)
            self.assertEqual(info["category"], "ai_tool_config")

    def test_detect_secret_references_uses_yaml_patterns(self):
        # Override secret pattern is applied in detect_secret_references.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "rules").mkdir(parents=True)
            (state_dir / "rules" / "overrides.yaml").write_text(
                "secret_patterns:\n"
                "  - kind: 'Acme token'\n"
                "    pattern: 'acme_[A-Za-z0-9]{12,}'\n",
                encoding="utf-8",
            )
            secret_file = Path(tmp) / "config.env"
            secret_file.write_text(
                'export ACME_TOKEN="acme_abcdefghijklmnop1234"\n', encoding="utf-8"
            )
            findings = oa.detect_secret_references(secret_file, state_dir=state_dir)
            self.assertTrue(any(f["kind"] == "Acme token" for f in findings), findings)

    def test_load_rules_returns_independent_snapshots(self):
        # RuleSet is immutable; two loads must not alias the same lists.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            a = oa.load_rules(state_dir)
            b = oa.load_rules(state_dir)
            self.assertIsNot(a, b)
            self.assertEqual(a.saas_domains, b.saas_domains)
            self.assertEqual(len(a.path_rules), len(b.path_rules))


if __name__ == "__main__":
    unittest.main()
