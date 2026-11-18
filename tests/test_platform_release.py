"""Release contract tests with benign synthetic metadata; no platform writes."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "adapter"))
from build_release import write_skill
from platform_release import (digest, evidence_input, node_inputs, read_build,
                              registry_bundle, registry_lock, verify_download)
from worker import core_scripts


def locks():
    return {key: {"artifact_id": key + ".1", "version": "0.4.2", "platform": "linux",
        "arch": "x86_64", "artifact_type": "directory_tar_gz", "entrypoint": "isaac_scene_" + key,
        "sha256": hashlib.sha256(key.encode()).hexdigest()} for key in ("provider", "gateway")}


class PlatformReleaseTests(unittest.TestCase):
    def build_fixture(self, path):
        path.mkdir()
        (path / "source").mkdir()
        (path / "source/SKILL.md").write_text("Fixture only")
        nodes = locks()
        archives = {}
        for key, node in nodes.items():
            archive = path / f"{node['entrypoint']}-0.4.2-linux-x86_64.tar.gz"
            archive.write_bytes(key.encode())
            archives[archive.name] = {"sha256": node["sha256"], "bytes": len(key)}
        release = {"version": "0.4.2", "candidate": False,
            "source": {"repository": "https://github.com/example/scene", "commit": "a" * 40, "tag": "v0.4.2"},
            "sources": {"SKILL.md": digest(path / "source/SKILL.md")}, "nodes": nodes,
            "archives": archives, "upstream": {"gateway": "fixture"}}
        (path / "release.json").write_text(json.dumps(release))
        return release

    def test_packaged_core_is_self_contained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundled = root / "scene_core/scripts"
            bundled.mkdir(parents=True)
            (bundled / "scene_tools.py").write_text("# fixture")
            self.assertEqual(core_scripts({}, root / "worker.py"), bundled)
            with self.assertRaisesRegex(ValueError, "omit core_root"):
                core_scripts({"core_root": str(root / "other")}, root / "worker.py")

    def test_source_core_resolution(self):
        self.assertEqual(core_scripts({}), ROOT / "core/scripts")
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                core_scripts({}, Path(temp) / "worker.py")

    def test_build_candidate_and_modified_snapshot_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "build"
            release = self.build_fixture(root)
            self.assertEqual(read_build(root)[1]["version"], "0.4.2")
            release["candidate"] = True
            (root / "release.json").write_text(json.dumps(release))
            with self.assertRaisesRegex(ValueError, "candidate"):
                read_build(root)
            release["candidate"] = False
            (root / "release.json").write_text(json.dumps(release))
            (root / "source/SKILL.md").write_text("Updated fixture")
            with self.assertRaisesRegex(ValueError, "snapshot differs"):
                read_build(root)

    def test_node_inputs_are_outside_archives_and_do_not_register(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.build_fixture(root / "build")
            node_inputs(root / "build", root / "registration",
                {"provider": "provider.2", "gateway": "gateway.2"},
                {"provider": "operator-example", "gateway": "adapter-example"})
            row = json.loads((root / "registration/provider.json").read_text())
            self.assertEqual(row["metadata"]["artifact_id"], "provider.2")
            self.assertTrue((root / "registration" / row["bundle"]).is_file())
            self.assertEqual(row["metadata"]["github_url"], "https://github.com/example/scene")

    def test_registry_requires_every_exact_lock_field(self):
        node = locks()["provider"]
        self.assertEqual(registry_lock(node, node, node["artifact_id"]), node)
        self.assertEqual(registry_lock({"metadata": node, "status": "published"}, node, node["artifact_id"]), node)
        for field in node:
            changed = {**node, field: "different"}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "mismatch"):
                registry_lock(changed, node, node["artifact_id"])
        with self.assertRaisesRegex(ValueError, "not published"):
            registry_lock({**node, "status": "draft"}, node, node["artifact_id"])

    def test_download_is_bound_to_actual_bytes(self):
        class Response(io.BytesIO):
            def geturl(self):
                return "https://example.com/node.tar.gz"
        data = b"benign archive fixture"
        expected = hashlib.sha256(data).hexdigest()
        with patch("platform_release.urlopen", return_value=Response(data)):
            verify_download({"url": "https://example.com/node.tar.gz"}, expected, len(data))
        with patch("platform_release.urlopen", return_value=Response(data)):
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_download({"url": "https://example.com/node.tar.gz"}, expected, len(data) + 1)

    def test_skill_and_evidence_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_skill(root / "source", ROOT, locks(), "0.4.2", "registry")
            manifest = json.loads((root / "source/skill.yaml").read_text())
            self.assertEqual(manifest["artifacts"]["resolver"], "registry")
            self.assertEqual(len(manifest["required_tools"]), 8)
            self.assertTrue((root / "source/deployment.example.json").is_file())
            archive = root / "bundle.tar.gz"
            data = (root / "source/skill.yaml").read_bytes()
            inventory = json.dumps({"files": [{"path": "skill.yaml", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}]}).encode()
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(root / "source/skill.yaml", arcname="skill.yaml")
                info = tarfile.TarInfo("archive-manifest.json")
                info.size = len(inventory)
                tar.addfile(info, io.BytesIO(inventory))
            video = root / "fixture.mp4"
            video.write_bytes(b"not a real video; tests binding only")
            context = {"profile": "local-review", "core_version": "fixture", "environment": "fixture",
                       "task_goal": "fixture", "success_description": "fixture, not an actual run"}
            evidence_input(archive, video, digest(archive), context, root / "evidence")
            row = json.loads((root / "evidence/skill.json").read_text())
            self.assertEqual(row["metadata"]["forge_version"], 1)
            proof = json.loads((root / "evidence/evidence-record.json").read_text())
            self.assertEqual(proof["video_decode_check"], "not_run")
            with self.assertRaisesRegex(ValueError, "changed"):
                evidence_input(archive, video, "0" * 64, context, root / "other")
            with self.assertRaises(FileExistsError):
                write_skill(root / "source", ROOT, locks(), "0.4.2")

    @unittest.skipUnless((ROOT / ".deps/paos/scripts/package_skill.py").is_file(), "fetch pinned paos source")
    def test_registry_bundle_uses_official_packager(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = self.build_fixture(root / "build")
            for name in ("SKILL.md", "LICENSE", "NOTICE", "deployment.example.json"):
                path = root / "build/source" / name
                path.write_bytes((ROOT / name).read_bytes())
                release["sources"][name] = digest(path)
            (root / "build/release.json").write_text(json.dumps(release))
            ids = {key: node["artifact_id"] for key, node in release["nodes"].items()}
            with patch("platform_release.public_node", side_effect=lambda base, identity:
                       next(node for node in release["nodes"].values() if node["artifact_id"] == identity)), \
                 patch("platform_release.verify_download") as downloads:
                bundle = registry_bundle(root / "build", root / "output", ids,
                                         "https://example.com", ROOT / ".deps/paos")
            self.assertEqual(downloads.call_count, 2)
            with tarfile.open(bundle) as tar:
                self.assertIn("archive-manifest.json", tar.getnames())
                self.assertIn("skill.yaml", tar.getnames())
                manifest = json.load(tar.extractfile("skill.yaml"))
                self.assertEqual(manifest["artifacts"]["nodes"], release["nodes"])
                self.assertEqual(manifest["artifacts"]["resolver"], "registry")
            proof = json.loads((root / "output/release-proof.json").read_text())
            self.assertEqual(proof["bundle_sha256"], digest(bundle))
            self.assertFalse(proof["skill_published"])


if __name__ == "__main__":
    unittest.main()
