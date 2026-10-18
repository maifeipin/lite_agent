"""Independent regressions for acceptance failures that must not report success."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify import check_formatting


class TestAcceptanceCloseout(unittest.TestCase):
    def test_missing_git_cannot_report_format_success(self):
        with patch("scripts.verify.shutil.which", return_value=None):
            self.assertFalse(check_formatting(".")[0])

    def test_failed_untracked_listing_cannot_report_format_success(self):
        good = subprocess.CompletedProcess([], 0, "", "")
        bad = subprocess.CompletedProcess([], 128, "", "listing failed")
        with patch("scripts.verify.shutil.which", return_value="git"), patch(
            "scripts.verify.subprocess.run", side_effect=[good, good, bad]
        ):
            ok, error = check_formatting(".")
        self.assertFalse(ok)
        self.assertIn("listing failed", error)

    def test_acceptance_plugin_rejects_skips_but_accepts_success(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "test_probe.py"
            for body, expected in (
                ("import pytest\ndef test_probe(): pytest.skip('required dependency absent')\n", 1),
                ("def test_probe(): assert True\n", 0),
            ):
                file.write_text(body, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", str(file), "--confcutdir", td,
                     "-p", "dev.tests.support.no_skips", "-p", "no:cacheprovider", "-q"],
                    cwd=str(root), capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
