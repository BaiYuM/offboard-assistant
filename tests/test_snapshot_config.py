from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import offboard_assistant as core


class SnapshotConfigTests(unittest.TestCase):
    def test_relative_state_dir_quarantine_round_trip_uses_absolute_paths(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                state_dir = core.state_dir_from_arg("relative-state")
                self.assertTrue(state_dir.is_absolute())

                source = Path(tmp) / "cleanup-target.txt"
                source.write_text("payload", encoding="utf-8")
                batch = state_dir / "quarantine" / "20260713-120000"
                batch.mkdir(parents=True)
                destination = batch / "001-cleanup-target.txt"
                shutil.move(str(source), str(destination))
                (batch / "manifest.json").write_text(
                    json.dumps(
                        {
                            "items": [
                                {
                                    "source": str(source),
                                    "destination": str(destination),
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

                result = core.restore_quarantine_dir(batch)
                self.assertTrue(source.exists())
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["restored"], [str(source)])

    def test_collect_snapshot_applies_owner_config_and_disables_ide_scan(self) -> None:
        company_item = {
            "id": "company",
            "type": "environment_variable",
            "name": "WORK_ACCOUNT",
            "path": r"C:\config\user@corp.example",
            "account_owner_hint": "personal_account",
        }
        personal_item = {
            "id": "personal",
            "type": "installed_app",
            "name": "Personal account",
            "path": r"C:\config\me@personal.example",
            "account_owner_hint": "company_account",
        }
        config = {
            "ide_scan_enabled": False,
            "company_email_domains": ["@corp.example"],
            "personal_email_domains": ["@personal.example"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                mock.patch.object(core, "scan_environment", return_value=[company_item]),
                mock.patch.object(core, "scan_installed_apps_from_registry", return_value=[personal_item]),
                mock.patch.object(core, "scan_browser_logins", return_value=[]),
                mock.patch.object(core, "scan_sensitive_locations", return_value=[]) as sensitive_scan,
                mock.patch.object(core, "scan_chat_locations", return_value=[]),
                mock.patch.object(core, "scan_recent_ide_projects") as ide_scan,
            ):
                snapshot = core.collect_snapshot(state_dir, [], config=config)

        ide_scan.assert_not_called()
        sensitive_scan.assert_called_once_with([], state_dir=state_dir, config=config)
        hints = {item["id"]: item["account_owner_hint"] for item in snapshot["items"]}
        self.assertEqual(
            hints,
            {
                "company": "company_account",
                "personal": "personal_account",
            },
        )

    def test_scan_sensitive_locations_skips_excluded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            included = root / "included" / ".env"
            excluded_dir = root / "excluded"
            excluded = excluded_dir / ".env"
            included.parent.mkdir()
            excluded_dir.mkdir()
            included.write_text("INCLUDED=value\n", encoding="utf-8")
            excluded.write_text("EXCLUDED=value\n", encoding="utf-8")

            items = core.scan_sensitive_locations(
                [root],
                config={"excluded_paths": [str(excluded_dir)]},
            )

        paths = {Path(item["path"]) for item in items}
        self.assertIn(included.resolve(), paths)
        self.assertNotIn(excluded.resolve(), paths)

    def test_max_files_is_per_file_hard_limit_and_stops_later_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first_root = base / "first"
            later_root = base / "later"
            first_root.mkdir()
            later_root.mkdir()
            for path in (
                first_root / "README.md",
                first_root / ".env",
                first_root / "token.secret",
                later_root / ".env",
            ):
                path.write_text("value\n", encoding="utf-8")

            walked_roots: list[Path] = []

            def fixed_walk(root: Path):
                root = Path(root)
                walked_roots.append(root)
                if root == first_root:
                    return iter(
                        [
                            (
                                str(first_root),
                                [],
                                ["README.md", ".env", "token.secret"],
                            )
                        ]
                    )
                return iter([(str(later_root), [], [".env"])])

            with (
                mock.patch.object(core.os, "walk", side_effect=fixed_walk),
                mock.patch.object(core, "detect_secret_references", return_value=[]),
            ):
                items = core.scan_sensitive_locations(
                    [first_root, later_root],
                    max_files=2,
                )

        self.assertEqual(walked_roots, [first_root])
        self.assertEqual([Path(item["path"]) for item in items], [(first_root / ".env").resolve()])


class VersionTests(unittest.TestCase):
    def test_app_version_is_semantic_version(self) -> None:
        self.assertRegex(
            core.APP_VERSION,
            r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
        )

    def test_build_parser_version_output(self) -> None:
        parser = core.build_parser()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"{parser.prog} {core.APP_VERSION}")


if __name__ == "__main__":
    unittest.main()
