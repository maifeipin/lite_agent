"""Independent regressions for the stage-three export boundary."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


_SPEC = importlib.util.spec_from_file_location(
    "stage3_export_closeout", Path(__file__).resolve().parents[3] / "scripts/export_core.py"
)
exporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exporter)


class TestExportCloseout(unittest.TestCase):
    def test_destination_file_symlink_cannot_overwrite_external_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, out = root / "src", root / "out"
            src.mkdir()
            out.mkdir()
            (src / "main.py").write_text("replacement", encoding="utf-8")
            victim = root / "victim"
            victim.write_text("preserve", encoding="utf-8")
            (out / "main.py").symlink_to(victim)
            ok, code, _ = exporter.export_core_files(
                [("", "main.py")], str(src), str(out)
            )
            self.assertFalse(ok)
            self.assertEqual(code, 2)
            self.assertEqual(victim.read_text(encoding="utf-8"), "preserve")

    def test_parent_symlink_rejected_before_creating_external_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src, out, outside = root / "src", root / "out", root / "outside"
            (src / "link/nested").mkdir(parents=True)
            (src / "link/nested/module.py").write_text("pass", encoding="utf-8")
            out.mkdir()
            outside.mkdir()
            (out / "link").symlink_to(outside, target_is_directory=True)
            ok, code, _ = exporter.export_core_files(
                [("", "link/nested/module.py")], str(src), str(out)
            )
            self.assertFalse(ok)
            self.assertEqual(code, 2)
            self.assertEqual(list(outside.iterdir()), [])

    def test_strict_hash_mode_rejects_missing_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            items = []
            for name in sorted(exporter.REQUIRED_CORE_MODULES):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"content\n")
                items.append((hashlib.sha256(path.read_bytes()).hexdigest(), name))
            self.assertTrue(exporter.verify_manifest_items(items, str(root), True)[0])
            items[0] = ("", items[0][1])
            ok, code, _ = exporter.verify_manifest_items(items, str(root), True)
            self.assertFalse(ok)
            self.assertEqual(code, 3)
