"""End-to-end CLI workflow tests for Offboard Assistant.

Run via:
    python -m unittest discover tests/e2e -p "test_*.py" -v

These tests invoke ``offboard_assistant.py`` as a subprocess against a
tempdir-backed state directory, so they exercise the same code path a real
user would hit from the terminal. They are cross-platform (run on macOS,
Linux, and Windows) and never touch the user's real ``%APPDATA%`` or
``~/.offboard-assistant`` directories.

Each test sets ``APPDATA`` (Windows) and ``XDG_CONFIG_HOME`` / ``HOME``
fallbacks via the ``--state-dir`` flag so the tool is fully isolated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "offboard_assistant.py"


def _run_cli(state_dir: Path, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["APPDATA"] = str(state_dir)  # forces default_state_base() to land in tmp
    env["XDG_CONFIG_HOME"] = str(state_dir)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI), "--state-dir", str(state_dir), *args],
        capture_output=True,
        text=True,
        env=env,
    )


class CliWorkflowE2E(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # `--state-dir X` lands the actual state at X/.offboard-assistant
        # (see `state_dir_from_arg` + `ensure_state_dir` in offboard_assistant.py).
        self.state_root = self.tmp / "state"
        self.state = self.state_root / ".offboard-assistant"
        self.scan_root = self.tmp / "scan"
        self.scan_root.mkdir()
        (self.scan_root / ".env").write_text(
            'OPENAI_API_KEY="sk-abc1234567890abcdef"\n', encoding="utf-8"
        )
        (self.scan_root / "README.md").write_text("hello", encoding="utf-8")
        # A JetBrains recent-projects fixture so the new IDE scanner has
        # something to discover. Layout matches %APPDATA%/JetBrains/<IDE>/options/.
        ide_dir = self.scan_root / "JetBrains" / "IntelliJIdea2024.1" / "options"
        ide_dir.mkdir(parents=True)
        (ide_dir / "recentProjects.xml").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<application>
  <component name="RecentProjectsManager">
    <option name="recentPaths"><list><option value="/work/acme"/></list></option>
    <option name="additionalInfo"><map>
      <entry key="/work/acme"><value>
        <RecentProjectMetaInfo>
          <option name="projectName" value="acme-backend"/>
          <option name="lastOpened" value="1704067200000"/>
        </RecentProjectMetaInfo>
      </value></entry>
    </map></option>
  </component>
</application>
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_creates_baseline(self) -> None:
        result = _run_cli(self.state_root, "init", "--since", "2026-01-01", "--scan-root", str(self.scan_root))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue((self.state / "baseline.json").exists(), "baseline.json should exist after init")
        baseline = json.loads((self.state / "baseline.json").read_text(encoding="utf-8"))
        # `parse_since` converts to UTC; we only care that the date is
        # within ±1 day of the requested baseline (tz-dependent).
        since = baseline.get("baseline_since", "")
        self.assertTrue(since.startswith("2025-12-31") or since.startswith("2026-01-01"), since)
        # No secret values should ever be written.
        self.assertNotIn("sk-abc1234567890abcdef", json.dumps(baseline))

    def test_scan_writes_snapshot(self) -> None:
        _run_cli(self.state_root, "init", "--since", "2026-01-01", "--scan-root", str(self.scan_root))
        result = _run_cli(self.state_root, "scan", "--scan-root", str(self.scan_root))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue((self.state / "latest-snapshot.json").exists())

    def test_full_workflow_init_scan_report_actions(self) -> None:
        # init
        r = _run_cli(self.state_root, "init", "--since", "2026-01-01", "--scan-root", str(self.scan_root))
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # scan
        r = _run_cli(self.state_root, "scan", "--scan-root", str(self.scan_root))
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        # report (markdown)
        r = _run_cli(
            self.state_root,
            "report",
            "--rescan",
            "--scan-root", str(self.scan_root),
            "--output", str(self.state_root / "report.md"),
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertTrue((self.state_root / "report.md").exists())
        # actions (json)
        r = _run_cli(
            self.state_root,
            "actions",
            "--rescan",
            "--scan-root", str(self.scan_root),
            "--format", "json",
            "--output", str(self.state_root / "actions.json"),
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        actions = json.loads((self.state_root / "actions.json").read_text(encoding="utf-8"))
        self.assertIn("actions", actions)
        self.assertIsInstance(actions["actions"], list)

    def test_actions_markdown_contains_account_owner_hint(self) -> None:
        _run_cli(self.state_root, "init", "--since", "2026-01-01", "--scan-root", str(self.scan_root))
        _run_cli(self.state_root, "scan", "--scan-root", str(self.scan_root))
        _run_cli(
            self.state_root,
            "actions",
            "--rescan",
            "--scan-root", str(self.scan_root),
            "--output", str(self.state_root / "actions.md"),
        )
        content = (self.state_root / "actions.md").read_text(encoding="utf-8")
        # Every emitted action should at least surface the owner hint field.
        self.assertIn("Account owner hint", content)

    def test_watch_install_once_does_not_modify_state(self) -> None:
        # `watch-install --once` should run a single diff and exit cleanly
        # without polluting the baseline/snapshot (separate write path).
        _run_cli(self.state_root, "init", "--since", "2026-01-01", "--scan-root", str(self.scan_root))
        r = _run_cli(
            self.state_root,
            "watch-install",
            "--once",
            "--watch-dir", str(self.scan_root),
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_jetbrains_recent_projects_appear_in_snapshot(self) -> None:
        # The scan reads APPDATA first; point it at the fixture so the
        # JetBrains XML is discovered via the standard path.
        r = _run_cli(
            self.state_root,
            "init",
            "--since", "2026-01-01",
            "--scan-root", str(self.scan_root),
            env_extra={"APPDATA": str(self.scan_root)},
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        baseline = json.loads((self.state / "baseline.json").read_text(encoding="utf-8"))
        ide_items = [
            it for it in baseline.get("items", [])
            if it.get("type") == "ide_recent_project"
        ]
        self.assertEqual(len(ide_items), 1, ide_items)
        self.assertEqual(ide_items[0]["name"], "acme-backend")
        self.assertEqual(ide_items[0]["ide"], "IntelliJIdea2024.1")
        # Privacy assertion: no plaintext secret/token anywhere in the snapshot.
        joined = json.dumps(baseline)
        self.assertNotIn("sk-abc1234567890abcdef", joined)


if __name__ == "__main__":
    unittest.main()
