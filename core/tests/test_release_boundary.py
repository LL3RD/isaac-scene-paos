"""Ordinary local portability and artifact-ownership regression checks."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import scene_tools
from isaac_scene.doctor import inspect
from isaac_scene.handoff import export_handoff, verify_handoff
from isaac_scene.output_policy import check_cli_outputs, new_output


class ReleaseBoundaryTests(unittest.TestCase):
    def test_default_doctor_is_pending_without_network_configuration(self):
        config = scene_tools.load_config(ROOT / 'config/config.json')
        self.assertEqual(inspect(ROOT, config)['status'], 'pending')
        self.assertNotIn('remote', config['runtime'])

    def test_environment_configuration_is_recursive_and_explicit(self):
        with patch.dict(os.environ, {'SCENE_TEST_ROOT': '/example/assets'}):
            self.assertEqual(scene_tools._expand_config({'libraries': ['${SCENE_TEST_ROOT}/usd']}),
                             {'libraries': ['/example/assets/usd']})

    def test_unset_environment_configuration_is_not_silently_accepted(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            scene_tools._expand_config('${SCENE_TEST_ROOT}')

    def test_unknown_named_profile_is_not_replaced_with_default(self):
        with self.assertRaises(ValueError):
            scene_tools.load_config(profile='unconfigured-test-machine')

    def test_output_accepts_a_new_revision_inside_task_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(new_output(Path(tmp)/'v2/scene.usda', root=tmp),
                             Path(tmp).resolve()/'v2/scene.usda')

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'scene.usda'
            source.write_text('existing artifact')
            with self.assertRaises(ValueError):
                new_output(source, root=tmp)
            self.assertEqual(source.read_text(), 'existing artifact')

    def test_cli_output_and_report_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp)/'report.json')
            with self.assertRaises(ValueError):
                check_cli_outputs(argparse.Namespace(output=target, report_output=target), {})

    def test_public_summary_has_no_internal_free_text_or_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root/'task.json'
            task.write_text(json.dumps({'delivery_scope': 'lookdev_prototype'}))
            result = export_handoff({'goal': 'internal research project', 'task_path': str(task),
                                     'selected_assets': [{'path': str(root/'asset.usd')}],
                                     'open_issues': ['internal note']},
                                    root/'handoff', config={'runtime': {'python': {'executable': sys.executable}}}, skill_root=ROOT)
            public = json.loads(Path(result['public_summary']).read_text())
            self.assertEqual(set(public), {'schema_version', 'packaging_status', 'scene_acceptance',
                                          'selected_asset_count', 'open_issue_count', 'artifacts'})
            self.assertEqual(public['open_issue_count'], 1)
            self.assertNotIn('internal research project', json.dumps(public))
            self.assertNotIn(str(root), json.dumps(public))
            self.assertEqual(verify_handoff(result['manifest_path'])['manifest']['delivery_scope'], 'lookdev_prototype')
            self.assertEqual(Path(result['profile_snapshot']).stat().st_mode & 0o777, 0o600)

    def test_shared_asset_library_is_optional_in_domain_discovery(self):
        result = inspect(ROOT, {'deployment_configured': True, 'runtime': {}, 'libraries': []})
        row = next(row for row in result['checks'] if row['name'] == 'domain_library_contract')
        self.assertEqual(row['status'], 'warn')

    def test_release_source_allowlist_matches_distribution(self):
        from package_release import audit
        self.assertEqual(audit(ROOT)['status'], 'pass')

    def test_packaged_source_can_be_verified_after_extraction(self):
        import zipfile
        from package_release import audit, package
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp)/'release.zip'
            package(archive, ROOT)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(Path(tmp)/'unpacked')
            unpacked = Path(tmp)/'unpacked/isaac-scene-skill-lite'
            self.assertEqual(audit(unpacked), audit(ROOT))


if __name__ == '__main__':
    unittest.main()
