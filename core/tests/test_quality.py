from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from isaac_scene.image_geometry import inspect_content_frame  # noqa: E402
from isaac_scene.quality import asset_quality_from_spec, benchmark_reuse_metrics, compare_images, evaluate_scene  # noqa: E402


class QualityTests(unittest.TestCase):
    def test_asset_quality_rejects_anchor_proxy_and_measures_diversity(self) -> None:
        spec = {
            "entities": [
                {"id": "chair_a", "role": "chair", "path": "/assets/chair.usd", "source": "retrieved"},
                {"id": "chair_b", "role": "chair", "path": "/assets/chair.usd", "source": "retrieved"},
                {"id": "meeting_table", "role": "table", "source": "primitive_proxy"},
                {"id": "wall", "role": "shell", "source": "authored_shell"},
            ]
        }
        quality = asset_quality_from_spec(spec)
        self.assertEqual(2, quality["retrieved_count"])
        self.assertEqual(1, quality["proxy_count"])
        self.assertEqual(0.5, quality["asset_diversity"])
        self.assertAlmostEqual(2 / 3, quality["anchor_real_asset_coverage"])

    def test_image_comparison_prefers_identical_image(self) -> None:
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            red = root / "red.png"
            blue = root / "blue.png"
            Image.new("RGB", (64, 48), (180, 30, 20)).save(red)
            Image.new("RGB", (64, 48), (20, 30, 180)).save(blue)
            same = compare_images(red, red)
            different = compare_images(red, blue)
            self.assertGreater(same["score"], different["score"])
            self.assertEqual("pass", same["status"])

    def test_letterbox_is_detected_and_removed_before_comparison(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except Exception:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            content = Image.new("RGB", (200, 100), (70, 90, 110))
            draw = ImageDraw.Draw(content)
            draw.rectangle((20, 15, 80, 85), fill=(180, 40, 20))
            draw.rectangle((120, 25, 180, 75), fill=(20, 170, 80))
            render = root / "render.png"
            reference = root / "reference.png"
            content.save(render)
            canvas = Image.new("RGB", (200, 150), (255, 255, 255))
            canvas.paste(content, (0, 25))
            canvas.save(reference)
            frame = inspect_content_frame(reference)
            self.assertTrue(frame["border_detected"])
            self.assertEqual([0, 25, 200, 125], frame["content_bbox_px"])
            self.assertAlmostEqual(2.0, frame["content_aspect_ratio"])
            result = compare_images(reference, render)
            self.assertEqual("pass", result["status"])
            self.assertGreater(result["score"], 0.99)

    def test_scene_quality_separates_geometry_from_visual_match(self) -> None:
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / "reference.png"
            render = root / "render.png"
            spec = root / "scene_spec.json"
            summary = root / "summary.json"
            Image.new("RGB", (32, 32), (180, 30, 20)).save(reference)
            Image.new("RGB", (32, 32), (20, 30, 180)).save(render)
            spec.write_text(json.dumps({"entities": [{"id": "sofa", "role": "sofa", "path": "/assets/sofa.usd"}]}))
            summary.write_text(json.dumps({"validation_pass": True, "facing_pass": True, "render_feedback_pass": True}))
            result = evaluate_scene(reference_image=reference, rendered_image=render, scene_spec=spec, summary=summary)
            self.assertTrue(result["gates"]["geometry_pass"])
            self.assertFalse(result["gates"]["reference_match_pass"])
            self.assertEqual("fail", result["status"])

    def test_missing_image_is_not_misreported_as_low_similarity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing_reference = root / "missing-reference.png"
            missing_render = root / "missing-render.png"
            result = evaluate_scene(
                reference_image=missing_reference,
                rendered_image=missing_render,
            )
            codes = {row["code"] for row in result["failures"]}
            self.assertIn("REFERENCE_MATCH_INPUT_MISSING", codes)
            self.assertNotIn("REFERENCE_MATCH_LOW", codes)

    def test_reference_match_rejects_any_visible_proxy(self) -> None:
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "image.png"
            spec = root / "scene_spec.json"
            summary = root / "summary.json"
            Image.new("RGB", (32, 32), (80, 90, 100)).save(image)
            spec.write_text(json.dumps({
                "entities": [
                    {"id": "chair", "role": "chair", "path": "/assets/chair.usd"},
                    {"id": "decor_box", "role": "decor", "source": "primitive_proxy"},
                ]
            }))
            summary.write_text(json.dumps({
                "validation_pass": True,
                "facing_pass": True,
                "render_feedback_pass": True,
            }))
            result = evaluate_scene(
                reference_image=image,
                rendered_image=image,
                scene_spec=spec,
                summary=summary,
            )
            self.assertFalse(result["gates"]["proxy_free_pass"])
            self.assertIn("VISIBLE_PROXY_ASSET_PRESENT", {
                failure["code"] for failure in result["failures"]
            })
            self.assertEqual("fail", result["status"])

    def test_reference_match_rejects_missing_anchor_definition(self) -> None:
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "image.png"
            spec = root / "scene_spec.json"
            summary = root / "summary.json"
            Image.new("RGB", (32, 32), (80, 90, 100)).save(image)
            spec.write_text(json.dumps({
                "entities": [
                    {"id": "carrier", "role": "carrier", "source": "authored_detail"},
                ]
            }))
            summary.write_text(json.dumps({
                "validation_pass": True,
                "facing_pass": True,
                "render_feedback_pass": True,
            }))
            result = evaluate_scene(
                reference_image=image,
                rendered_image=image,
                scene_spec=spec,
                summary=summary,
            )
            self.assertFalse(result["gates"]["anchor_definition_pass"])
            self.assertIn("ANCHOR_DEFINITION_MISSING", {
                failure["code"] for failure in result["failures"]
            })

    def test_benchmark_reuse_metrics_counts_cross_scene_reuse(self) -> None:
        result = benchmark_reuse_metrics([
            {"asset_quality": {"retrieved_paths": ["/a.usd", "/b.usd"]}},
            {"asset_quality": {"retrieved_paths": ["/a.usd", "/a.usd"]}},
        ])
        self.assertEqual(4, result["retrieved_instances"])
        self.assertEqual(2, result["unique_retrieved_assets"])
        self.assertEqual(2, result["cross_scene_repeated_instances"])
        self.assertEqual("/a.usd", result["most_reused_assets"][0]["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
