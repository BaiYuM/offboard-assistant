import datetime as dt
import json
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


if __name__ == "__main__":
    unittest.main()
