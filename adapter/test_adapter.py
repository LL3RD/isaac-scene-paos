"""Benign contract/lifecycle tests. Run in the pinned provider environment."""
import asyncio
import copy
import json
import sqlite3
import os
from pathlib import Path
import sys
import tempfile
import tarfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from argparse import Namespace

from forge_tool import (ToolContext, ToolEndpointError, ToolExecutionKey, ToolRequest,
                        make_invoke_request_envelope)
from forge_tool.dora import tool_envelope_to_message, tool_message_to_envelope
from forge_msgs import ToolMessage
from jsonschema import Draft202012Validator, ValidationError
from contracts import ENDPOINT, IMPLEMENTATION, SCHEMAS, output_schema, tool_specs, validate
from node import SceneBinding, make_handler, write_profile
from service import Operation, SceneService, worker_environment
from lifecycle import JOB_MARKER, JobProcesses, identity
from build_release import portable_member
import build_release
from catalog import Catalog

LAYOUT = {"floor_size_m": [5, 5], "camera": {"position_m": [3, -4, 3], "look_at_m": [0, 0, 1], "focal_length_mm": 28},
          "entities": [{"id": "bench", "asset_id": "bench", "position_m": [0, 0, 0], "yaw_deg": 0, "support_id": "floor"}]}


class Events:
    async def emit(self, event):
        pass


def context(op, identity="invocation"):
    return ToolContext(execution_key=ToolExecutionKey(identity, "attempt"), tool_id="isaac." + op.replace("_", ".", 1),
                       implementation_id=IMPLEMENTATION, endpoint_id=ENDPOINT, operation=op)


class ContractTests(unittest.TestCase):
    def test_release_provenance_requires_clean_tagged_source(self):
        commit = "a" * 40
        with patch.object(build_release, "VERSION", "0.4.1"), patch.object(
                build_release, "git", side_effect=[commit, "", commit]):
            self.assertEqual(build_release.provenance("https://github.com/example/scene", "v0.4.1")["commit"], commit)
        with patch.object(build_release, "VERSION", "0.4.1"), patch.object(
                build_release, "git", side_effect=[commit, " M worker.py"]):
            with self.assertRaisesRegex(ValueError, "Commit"):
                build_release.provenance("https://github.com/example/scene", "v0.4.1")

    def test_development_version_is_not_a_release(self):
        with patch.object(build_release, "VERSION", "0.4.1-dev"):
            with self.assertRaisesRegex(ValueError, "development"):
                build_release.provenance("https://github.com/example/scene", "v0.4.1-dev")

    def test_catalog_filters_and_solve_contract(self):
        validate("asset_search", {"query": "computer", "source": "synthesis", "canonical_class": "electronics", "max_size_m": [1, 1, 1]})
        validate("scene_solve", {"scene_id": "a" * 32, "entity_ids": ["bench"]})
        with self.assertRaises(ValueError):
            validate("scene_build", {"layout": {**LAYOUT, "camera": {**LAYOUT["camera"], "focal_length_mm": float("nan")}}})

    def test_archive_headers_omit_builder_identity(self):
        info = tarfile.TarInfo("provider/library.so")
        info.uid, info.gid, info.uname, info.gname = 1000, 1000, "builder", "staff"
        info.mtime, info.mode, info.pax_headers = 123, 0o775, {"mtime": "123"}
        cleaned = portable_member(info)
        self.assertEqual((cleaned.uid, cleaned.gid, cleaned.mtime), (0, 0, 0))
        self.assertEqual((cleaned.uname, cleaned.gname, cleaned.pax_headers), ("", "", {}))
        self.assertEqual(cleaned.mode, 0o755)

    def test_external_worker_environment(self):
        original = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/bundle/libraries",
                    "LD_LIBRARY_PATH_ORIG": "/runtime/libraries", "PAOS_RUNTIME_INSTANCE_ID": "fixture-run"}
        frozen = worker_environment(original, frozen=True)
        self.assertEqual(frozen["LD_LIBRARY_PATH"], "/runtime/libraries")
        self.assertEqual(frozen["PAOS_RUNTIME_INSTANCE_ID"], "fixture-run")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", frozen)
        self.assertEqual(worker_environment(original, frozen=False)["LD_LIBRARY_PATH"], "/bundle/libraries")
        self.assertNotIn("LD_LIBRARY_PATH", worker_environment({"LD_LIBRARY_PATH": "/bundle"}, frozen=True))

    def test_all_specs_have_strict_schemas(self):
        self.assertEqual(len(tool_specs()), 8)
        for spec in tool_specs():
            self.assertFalse(spec["input_schema"]["additionalProperties"])
            self.assertFalse(spec["output_schema"]["additionalProperties"])

    def test_source_profile_uses_bounded_tool_queues_and_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_profile(Namespace(write_profile=root / "profile", config=root / "deployment.json",
                python=Path(sys.executable), gateway=root / "gateway.py", port=19042))
            dataflow = json.loads((root / "profile/dataflow.yaml").read_text())
            for node in dataflow["nodes"]:
                tool_inputs = [v for k, v in node["inputs"].items() if k != "heartbeat"]
                self.assertEqual(tool_inputs[0]["queue_size"], 128)
            self.assertEqual(json.loads((root / "profile/gateway.yaml").read_text())["host"], "127.0.0.1")
            zenoh = json.loads((root / "profile/zenoh.json").read_text())
            self.assertFalse(zenoh["scouting"]["multicast"]["enabled"])

    def test_metres_camera_and_layout(self):
        validate("scene_build", {"layout": LAYOUT})

    def test_validation_warning_is_not_execution_failure(self):
        Draft202012Validator(output_schema("scene_validate")).validate({
            "scene_id": "a" * 32, "artifacts": [], "delivery_scope": "review_scene",
            "checks": {"dependencies": "warn", "support": "pass", "physics": "not_run", "reachability": "not_run"}})

    def test_mistyped_units_are_rejected(self):
        value = copy.deepcopy(LAYOUT)
        value["entities"][0]["position_m"] = ["one", 0, 0]
        with self.assertRaises(ValidationError):
            validate("scene_build", {"layout": value})

    def test_limits_are_enforced(self):
        with self.assertRaises(ValidationError):
            validate("asset_search", {"query": "bench", "limit": 21})
        with self.assertRaises(ValidationError):
            validate("scene_render", {"scene_id": "a" * 32, "profile": "final"})


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        fixture = root / "bench.usda"
        fixture.write_text('#usda 1.0\n(defaultPrim="Bench"\nmetersPerUnit=1\nupAxis="Z")\ndef Xform "Bench" {}\n')
        self.config_path = root / "deployment.json"
        self.config_path.write_text(json.dumps({"work_root": str(root / "jobs"), "isaac_python": sys.executable,
            "gpu_lock": str(root / "gpu.lock"), "assets": {"bench": {"path": str(fixture), "label": "Bench", "tags": ["table"]}}}))
        self.service = SceneService(self.config_path)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_search_only_public_fields(self):
        result = self.service.query("asset_search", {"query": "table"})
        self.assertEqual(result["assets"][0]["asset_id"], "bench")
        self.assertNotIn("path", result["assets"][0])

    async def test_support_graph_and_asset_identity(self):
        value = copy.deepcopy(LAYOUT)
        value["entities"][0]["support_id"] = "bench"
        with self.assertRaises(ValueError):
            self.service.validate_request("scene_build", {"layout": value})
        value["entities"][0]["asset_id"] = "unregistered_bench"
        with self.assertRaises(ValueError):
            self.service.validate_request("scene_build", {"layout": value})


    async def test_solver_rejects_unknown_support_mode(self):
        self.service.db.execute("INSERT INTO scenes VALUES (?,?,?,?)", ("a" * 32, None, json.dumps(LAYOUT), "fixture"))
        self.service.db.commit()
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_MOUNT_MODE"):
            self.service.validate_request("scene_solve", {"scene_id": "a" * 32})
        with self.assertRaisesRegex(ValueError, "INVALID_SOLVE_SELECTION"):
            self.service.validate_request("scene_solve", {"scene_id": "a" * 32, "entity_ids": ["absent"]})

    async def test_single_owner(self):
        with self.assertRaises(BlockingIOError):
            SceneService(self.config_path)

    async def test_arrow_query_contract(self):
        handler = make_handler(self.service)
        binding = SceneBinding(handler, self.service)
        envelope = make_invoke_request_envelope(ToolRequest({"query": "bench"}), context("asset_search"),
            request_id="query-one", endpoint_instance_id=handler.endpoint_instance_id)
        replies = await binding.dispatch_input(tool_envelope_to_message(envelope).to_arrow())
        reply = tool_message_to_envelope(ToolMessage.from_arrow(replies[0]))
        self.assertEqual(reply.message_type, "tool.invoke.response")
        self.assertEqual(reply.payload["result"]["status"], "succeeded")

    async def test_queued_cancel_and_idempotent_acceptance(self):
        await self.service.slots.acquire()
        await self.service.slots.acquire()
        op = Operation(self.service, "scene_build")
        ctx = context("scene_build")
        request = ToolRequest({"layout": LAYOUT})
        first = await op.start(request, ctx, Events())
        second = await op.start(request, ctx, Events())
        self.assertEqual(first.details["job_id"], second.details["job_id"])
        self.assertTrue(second.details["reused"])
        self.assertEqual((await op.cancel(ctx.execution_key)).status, "accepted")
        await asyncio.gather(*list(self.service.tasks.values()))
        self.assertEqual((await op.status(ctx.execution_key)).phase, "cancelled")
        self.assertEqual((await op.result(ctx.execution_key)).result.status, "cancelled")
        self.assertFalse((self.service.root / first.details["job_id"] / "process.json").exists())
        self.service.slots.release()
        self.service.slots.release()

    async def test_argument_identity(self):
        key = context("scene_build").execution_key
        fingerprint, _ = self.service.check_identity("scene_build", {"layout": LAYOUT}, key)
        self.service.db.execute("INSERT INTO jobs VALUES ('job',?,?,?,?, 'cancelled',NULL,NULL)",
            (key.invocation_id, key.attempt_id, fingerprint, "scene_build"))
        self.service.db.commit()
        updated = copy.deepcopy(LAYOUT)
        updated["floor_size_m"] = [6, 6]
        with self.assertRaises(ToolEndpointError):
            self.service.check_identity("scene_build", {"layout": updated}, key)

    async def test_restart_keeps_unknown_without_rerun(self):
        self.service.db.execute("INSERT INTO jobs VALUES ('job','previous','attempt','hash','scene_build','running',NULL,NULL)")
        self.service.db.commit()
        await self.service.close()
        self.service = SceneService(self.config_path)
        row = self.service.row(ToolExecutionKey("previous", "attempt"))
        self.assertEqual(row["phase"], "unknown")
        self.assertFalse(self.service.tasks)

    async def test_expired_job_does_not_start_worker(self):
        ctx = ToolContext(execution_key=ToolExecutionKey("expired", "attempt"), tool_id="isaac.scene.build",
            implementation_id=IMPLEMENTATION, endpoint_id=ENDPOINT, operation="scene_build",
            deadline_ms=int(time.time() * 1000) - 100)
        op = Operation(self.service, "scene_build")
        result = await op.start(ToolRequest({"layout": LAYOUT}), ctx, Events())
        await asyncio.gather(*list(self.service.tasks.values()))
        self.assertEqual((await op.status(ctx.execution_key)).error.code, "JOB_DEADLINE_EXCEEDED")
        self.assertFalse((self.service.root / result.details["job_id"] / "process.json").exists())

    async def test_cleanup_failure_blocks_new_work(self):
        ctx = context("scene_build")
        await self.service.slots.acquire()
        await self.service.slots.acquire()
        with patch("service.JobProcesses.terminate", new=AsyncMock(return_value={123: "fixture"})):
            op = Operation(self.service, "scene_build")
            await op.start(ToolRequest({"layout": LAYOUT}), ctx, Events())
            await op.cancel(ctx.execution_key)
            await asyncio.gather(*list(self.service.tasks.values()))
            self.assertEqual((await op.status(ctx.execution_key)).error.code, "PROCESS_CLEANUP_FAILED")
            with self.assertRaises(ToolEndpointError):
                await op.start(ToolRequest({"layout": LAYOUT}), context("scene_build", "next"), Events())
        self.service.slots.release()
        self.service.slots.release()

    async def test_running_cancel_reaps_detached_child_and_releases_gpu(self):
        """Ordinary renderer stand-in: a worker with a detached helper."""
        real_spawn = asyncio.create_subprocess_exec
        fixture = Path(self.temp.name) / "helper.pid"
        program = ("import os, subprocess, sys, time; from pathlib import Path; "
                   "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                   "start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)")

        async def spawn(*args, **kwargs):
            return await real_spawn(sys.executable, "-c", program, str(fixture), **kwargs)

        self.service.db.execute("INSERT INTO scenes VALUES (?,?,?,?)", ("a" * 32, None, "{}", "fixture"))
        self.service.db.commit()
        op = Operation(self.service, "scene_render")
        ctx = context("scene_render")
        try:
            with patch("service.asyncio.create_subprocess_exec", side_effect=spawn):
                result = await op.start(ToolRequest({"scene_id": "a" * 32, "profile": "review"}), ctx, Events())
                deadline = time.monotonic() + 5
                while not fixture.exists():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(.05)
                pid = int(fixture.read_text())
                self.assertIsNotNone(identity(pid))
                await op.cancel(ctx.execution_key)
                await asyncio.gather(*list(self.service.tasks.values()))
                self.assertIsNone(identity(pid))
                self.assertEqual((await op.status(ctx.execution_key)).phase, "cancelled")
                report = json.loads((self.service.root / result.details["job_id"] / "cleanup.private.json").read_text())
                self.assertEqual(report["status"], "pass")
                self.assertIn(pid, [r["pid"] for r in report["observed"]])
                import fcntl
                with Path(self.service.config["gpu_lock"]).open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            self.service.cancelled.update(self.service.tasks)
            await asyncio.gather(*list(self.service.tasks.values()), return_exceptions=True)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.asset = self.root / "object.usda"
        self.asset.write_text('#usda 1.0\n')
        self.path = self.root / "objects.sqlite"
        self.db = sqlite3.connect(self.path)
        self.db.execute("""CREATE TABLE objects (logical_id TEXT, model_id TEXT, callable INT,
            qa_status TEXT, license_review_status TEXT, allowed_use_scope TEXT, asset_scopes_json TEXT,
            review_report_json TEXT, bbox_m_json TEXT, semantic_name TEXT, aliases_json TEXT,
            canonical_class TEXT, subtype TEXT, front_axis TEXT, front_axis_status TEXT,
            attribution_text TEXT, wrapper_path TEXT)""")
        self.db.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "synthesis28_objects:example", "example", 1, "ready", "verified", "research",
            '["independent_object"]', '{"support_mode":"surface"}', '{"size":[0.3,0.2,0.1]}',
            "laptop", '[]', "electronics/computer", "laptop", "+Y", "verified", "Fixture author", str(self.asset)))
        self.db.commit()
        self.catalog = Catalog({"catalogs": {"synthesis": {"path": str(self.path),
            "allowed_use_scope": "research", "entrypoint_roots": [str(self.root)]}}})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_controlled_retrieval_has_provenance_and_no_paths(self):
        result = self.catalog.search({"query": "laptop", "source": "synthesis"})
        Draft202012Validator(output_schema("asset_search")).validate(result)
        self.assertEqual(len(result["assets"]), 1)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(result["assets"][0]["provenance"]["attribution"], "Fixture author")

    def test_filters_and_live_revocation(self):
        self.assertFalse(self.catalog.search({"query": "laptop", "max_size_m": [.1, .1, .1]})["assets"])
        self.assertFalse(self.catalog.search({"query": "laptop", "canonical_class": "industrial"})["assets"])
        self.catalog.resolve("synthesis28_objects:example")
        self.db.execute("UPDATE objects SET callable=0")
        self.db.commit()
        self.assertFalse(self.catalog.search({"query": "laptop"})["assets"])
        with self.assertRaises(ValueError):
            self.catalog.resolve("synthesis28_objects:example")

    def test_non_object_scope_and_missing_entrypoint_are_not_candidates(self):
        self.db.execute("UPDATE objects SET asset_scopes_json='[\"scene_root\"]'")
        self.db.commit()
        self.assertFalse(self.catalog.search({"query": "laptop"})["assets"])
        self.db.execute("UPDATE objects SET asset_scopes_json='[\"independent_object\"]'")
        self.db.commit()
        self.asset.rename(self.root / "unavailable.usda")
        self.assertFalse(self.catalog.search({"query": "laptop"})["assets"])

    def test_fixed_mount_is_visible_but_not_surface_solvable(self):
        self.db.execute("UPDATE objects SET review_report_json='{\"support_mode\":\"ceiling\"}'")
        self.db.commit()
        result = self.catalog.search({"query": "laptop"})["assets"][0]
        self.assertFalse(result["placement_supported"])

    def test_semantic_catalog_requires_exact_reviewed_identity(self):
        self.db.execute("""CREATE TABLE semantic_assets (logical_id TEXT, callable INT, availability TEXT,
            source_library TEXT, license_status TEXT, bbox_json TEXT, semantic_name TEXT, aliases_json TEXT,
            canonical_class TEXT, subtype TEXT, front_axis TEXT, source_asset_id TEXT, source_path TEXT)""")
        self.db.execute("INSERT INTO semantic_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "indexed:bench", 1, "local", "example_library", "existing_local_asset", '{"size":[2,1,1]}',
            "bench", '[]', "furniture/surface/workbench", "bench", "+Y", "bench", str(self.asset)))
        self.db.commit()
        policy = {"path": str(self.path), "entrypoint_roots": [str(self.root)], "approved_assets": {}}
        catalog = Catalog({"catalogs": {"semantic": policy}})
        self.assertFalse(catalog.search({"query": "bench"})["assets"])
        policy["approved_assets"]["indexed:bench"] = {"asset_scope": "independent_object",
            "source_library": "example_library", "license_status": "existing_local_asset",
            "support_mode": "floor", "allowed_use_scope": "research", "attribution": "Fixture"}
        self.assertEqual(catalog.resolve("indexed:bench")["support_mode"], "floor")
        self.db.execute("UPDATE semantic_assets SET availability='native_usd_scene'")
        self.db.commit()
        self.assertFalse(catalog.search({"query": "bench"})["assets"])

    def test_catalog_is_opened_read_only(self):
        before = self.path.read_bytes()
        self.catalog.search({"query": "laptop"})
        self.catalog.resolve("synthesis28_objects:example")
        self.assertEqual(before, self.path.read_bytes())


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process ownership")
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_helper_cleanup_keeps_other_job_alive(self):
        tracker = JobProcesses(uuid.uuid4().hex)
        unrelated = JobProcesses(uuid.uuid4().hex)
        processes = []
        try:
            for owner in (tracker, unrelated):
                env = {**os.environ, JOB_MARKER: owner.marker.decode().split("=", 1)[1]}
                process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
                    env=env, start_new_session=True)
                processes.append(process)
            self.assertIn(processes[0].pid, tracker.snapshot())
            self.assertNotIn(processes[1].pid, tracker.snapshot())
            self.assertEqual(await tracker.terminate(), {})
            self.assertIsNone(identity(processes[0].pid))
            self.assertIsNotNone(identity(processes[1].pid))
        finally:
            await tracker.terminate()
            await unrelated.terminate()
            for process in processes:
                await process.wait()

    async def test_term_then_kill_and_no_stale_identity_signal(self):
        tracker = JobProcesses(uuid.uuid4().hex)
        env = {**os.environ, JOB_MARKER: tracker.marker.decode().split("=", 1)[1]}
        process = await asyncio.create_subprocess_exec(sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
            env=env, stdout=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            self.assertEqual(await process.stdout.readline(), b"ready\n")
            tracker.send(process.pid, "different-start-time", 15)
            self.assertIsNotNone(identity(process.pid))
            self.assertEqual(await tracker.terminate(grace_s=.1), {})
            await process.wait()
            self.assertEqual(process.returncode, -9)
        finally:
            await tracker.terminate()
            await process.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
