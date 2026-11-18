from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = SKILL_ROOT / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from isaac_scene.render_profiles import load_render_profiles, path_tracing_settings, resolve_render_profile


class RenderProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SKILL_ROOT / "config" / "render_profiles.json"

    def test_three_profiles_are_explicit_and_ordered(self) -> None:
        profiles = load_render_profiles(self.config)
        self.assertEqual(["preview", "review", "final"], list(profiles))
        self.assertEqual("RayTracedLighting", profiles["preview"]["renderer"])
        self.assertEqual("PathTracing", profiles["final"]["renderer"])

    def test_cli_overrides_do_not_mutate_profile_contract(self) -> None:
        resolved = resolve_render_profile(self.config, "preview", width=800, height=450)
        self.assertEqual(800, resolved["width"])
        self.assertEqual(450, resolved["height"])
        self.assertEqual(960, load_render_profiles(self.config)["preview"]["width"])

    def test_path_tracing_settings_are_final_only(self) -> None:
        review = resolve_render_profile(self.config, "review")
        final = resolve_render_profile(self.config, "final")
        self.assertEqual({}, path_tracing_settings(review))
        settings = path_tracing_settings(final)
        self.assertEqual(1024, settings["/rtx/pathtracing/totalSpp"])
        self.assertTrue(settings["/rtx/pathtracing/optixDenoiser/enabled"])


if __name__ == "__main__":
    unittest.main()
