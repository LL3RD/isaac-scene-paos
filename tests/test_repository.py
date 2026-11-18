"""Packaging and generated-fixture checks; no GPU or proprietary libraries."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from repository import audit, export_source, source_files
from smoke import prepare


class RepositoryTests(unittest.TestCase):
    def test_locked_core_and_source_boundary(self):
        report = audit()
        self.assertEqual(report["core_files"], 196)
        self.assertFalse(any(Path(n).parts[0] in {".deps", ".local", ".venv", ".git"} for n in source_files()))

    def test_source_export_repeats_and_excludes_git(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "a.zip", Path(directory) / "b.zip"
            self.assertEqual(export_source(first)["sha256"], export_source(second)["sha256"])
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertIn("isaac-scene-paos/LICENSE", names)
                self.assertIn("isaac-scene-paos/source-manifest.json", names)
                self.assertFalse(any("/.git/" in n or "/.local/" in n for n in names))

    def test_fixture_is_self_contained_and_non_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            output, layout = prepare(Path(directory) / "example", sys.executable)
            config = json.loads((output / "deployment.json").read_text())
            self.assertEqual(len(layout["entities"]), 3)
            self.assertEqual(Path(config["core_root"]), ROOT / "core")
            for asset in config["assets"].values():
                text = Path(asset["path"]).read_text()
                self.assertIn('def Mesh "Body"', text)
                self.assertNotIn("references", text)
            with self.assertRaises(FileExistsError):
                prepare(output, sys.executable)

    def test_export_can_be_checked_after_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "source.zip"
            export_source(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(directory)
            unpacked = Path(directory) / "isaac-scene-paos"
            subprocess.run(["git", "init", "--quiet", str(unpacked)], check=True)
            result = subprocess.run([sys.executable, str(unpacked / "scripts/repository.py"), "audit"],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
