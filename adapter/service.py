"""Durable, single-owner executor; transport stays in node.py, USD stays in worker.py.

Trusted single-user local deployment, not an OS sandbox. SQLite records public IDs;
private jobs contain actual paths and logs. Only uniquely marked job processes may
be cancelled. A per-workspace owner lock prevents competing providers.
"""
import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from forge_tool import (ToolAccepted, ToolControlResponse, ToolEndpointError, ToolError,
                        ToolEvent, ToolExecutionStatus, ToolResult, ToolResultResponse)
from contracts import QUERIES, output_schema, validate
from jsonschema import Draft202012Validator
from lifecycle import JOB_MARKER, JobProcesses
from catalog import Catalog


def dump(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)


def error(code):
    return ToolError(code=code, message=code.replace("_", " ").lower(), retryable=False)


def worker_environment(environ=None, *, frozen=None):
    """Allowlist child environment; preserve ownership, restore external libraries."""
    environ = os.environ if environ is None else environ
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    env = {k: environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL",
                                  "PAOS_RUNTIME_INSTANCE_ID") if k in environ}
    library_path = environ.get("LD_LIBRARY_PATH_ORIG" if frozen else "LD_LIBRARY_PATH", "")
    if library_path:
        env["LD_LIBRARY_PATH"] = library_path
    return env


class SceneService:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text())
        self.root = Path(self.config["work_root"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = (self.root / "owner.lock").open("a+")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.owner.close()
            raise
        self.db = sqlite3.connect(self.root / "ledger.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, invocation TEXT UNIQUE,
            attempt TEXT, fingerprint TEXT, operation TEXT, phase TEXT, output TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS scenes (id TEXT PRIMARY KEY, parent TEXT, layout TEXT, job TEXT);
          CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, job TEXT, name TEXT, role TEXT, size INT, sha TEXT);
        """)
        # Never rerun an interrupted job implicitly. Old provider instance cannot
        # attest that a previous side effect completed; reconcile as unknown.
        self.db.execute("UPDATE jobs SET phase='unknown', error='OUTCOME_UNKNOWN' WHERE phase IN ('accepted','running','stopping')")
        self.db.commit()
        self.tasks = {}
        self.cancelled = set()
        self.pending_cleanup = {}
        self.slots = asyncio.Semaphore(2)
        self.assets = self.config["assets"]
        self.catalog = Catalog(self.config)
        for asset_id, asset in self.assets.items():
            validate("scene_build", {"layout": {"entities": [{"id": "object", "asset_id": asset_id,
                "position_m": [0, 0, 0], "yaw_deg": 0, "support_id": "floor"}],
                "floor_size_m": [4, 4], "camera": {"position_m": [3, -4, 3], "look_at_m": [0, 0, 1], "focal_length_mm": 28}}})
            if not Path(asset["path"]).is_file():
                raise ValueError("registered asset is unavailable")

    def check_identity(self, op, arguments, key):
        fingerprint = hashlib.sha256(json.dumps([op, arguments], sort_keys=True, allow_nan=False).encode()).hexdigest()
        old = self.db.execute("SELECT * FROM jobs WHERE invocation=?", (key.invocation_id,)).fetchone()
        if old and (old["attempt"] != key.attempt_id or old["fingerprint"] != fingerprint):
            raise ToolEndpointError(error("INVOCATION_CONFLICT"))
        return fingerprint, old

    def row(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE invocation=? AND attempt=?",
                              (key.invocation_id, key.attempt_id)).fetchone()
        if not row:
            raise ToolEndpointError(error("INVOCATION_NOT_FOUND"))
        return row

    def scene(self, scene_id):
        row = self.db.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not row:
            raise ValueError("SCENE_NOT_FOUND")
        return row

    def validate_request(self, op, arguments):
        validate(op, arguments)
        if "scene_id" in arguments:
            self.scene(arguments["scene_id"])
        if "layout" in arguments:
            entities = arguments["layout"]["entities"]
            ids = [e["id"] for e in entities]
            if len(ids) != len(set(ids)) or "floor" in ids:
                raise ValueError("DUPLICATE_OBJECT_ID")
            for entity in entities:
                asset = self.catalog.resolve(entity["asset_id"])
                if asset["source"] != "operator_registry" and not asset["placement_supported"]:
                    raise ValueError("UNSUPPORTED_MOUNT_MODE")
            parents = {e["id"]: e["support_id"] for e in entities}
            for entity in ids:
                seen = set()
                while entity != "floor":
                    if entity not in parents or entity in seen:
                        raise ValueError("INVALID_SUPPORT_GRAPH")
                    seen.add(entity)
                    entity = parents[entity]
            camera = arguments["layout"]["camera"]
            if camera["position_m"] == camera["look_at_m"]:
                raise ValueError("INVALID_CAMERA")
        if op == "scene_solve":
            layout = json.loads(self.scene(arguments["scene_id"])["layout"])
            if not layout["entities"] or set(arguments.get("entity_ids", [])) - {e["id"] for e in layout["entities"]}:
                raise ValueError("INVALID_SOLVE_SELECTION")
            for entity in layout["entities"]:
                if not self.catalog.resolve(entity["asset_id"])["placement_supported"]:
                    raise ValueError("UNSUPPORTED_MOUNT_MODE")

    def query(self, op, arguments):
        self.validate_request(op, arguments)
        if op == "asset_search":
            return self.catalog.search(arguments)
        if op == "scene_inspect":
            row = self.scene(arguments["scene_id"])
            return {"scene_id": row["id"], "parent_scene_id": row["parent"],
                    "layout": json.loads(row["layout"]),
                    "pending_ids": json.loads((self.root / row["job"] / "pending.json").read_text()),
                    "delivery_scope": "review_scene"}
        row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (arguments["artifact_id"],)).fetchone()
        if not row:
            raise ValueError("ARTIFACT_NOT_FOUND")
        offset, length = arguments.get("offset", 0), arguments.get("length", 32768)
        with (self.root / row["job"] / row["name"]).open("rb") as stream:
            stream.seek(offset)
            data = stream.read(length)
        return {"artifact_id": row["id"], "offset": offset, "total_bytes": row["size"],
                "eof": offset + len(data) >= row["size"], "base64": base64.b64encode(data).decode()}

    async def start(self, op, request, context, events):
        if self.pending_cleanup:
            raise ToolEndpointError(error("PROCESS_CLEANUP_REQUIRED"))
        try:
            self.validate_request(op, request.arguments)
        except Exception:
            raise ToolEndpointError(error("INVALID_ARGUMENTS")) from None
        fingerprint, old = self.check_identity(op, request.arguments, context.execution_key)
        if old:
            return ToolAccepted(details={"job_id": old["id"], "reused": True})
        if len(self.tasks) >= 8:
            raise ToolEndpointError(error("QUEUE_FULL"))
        job = uuid.uuid4().hex
        directory = self.root / job
        directory.mkdir(mode=0o700)
        data = {"operation": op, "arguments": request.arguments, "job_dir": str(directory)}
        if "scene_id" in request.arguments:
            scene = self.scene(request.arguments["scene_id"])
            data["source_dir"] = str(self.root / scene["job"])
        dump(directory / "request.json", data)
        self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,'accepted',NULL,NULL)",
                        (job, context.invocation_id, context.attempt_id, fingerprint, op))
        self.db.commit()
        task = asyncio.create_task(self.run(job, op, context, events))
        self.tasks[job] = task
        task.add_done_callback(lambda _: self.tasks.pop(job, None))
        return ToolAccepted(details={"job_id": job})

    def set_phase(self, job, phase, output=None, code=None):
        self.db.execute("UPDATE jobs SET phase=?, output=?, error=? WHERE id=?",
                        (phase, json.dumps(output) if output is not None else None, code, job))
        self.db.commit()

    async def run(self, job, op, context, events):
        directory = self.root / job
        process = None
        gpu = None
        processes = JobProcesses(job)
        phase, code, outputs = "failed", "EXECUTION_FAILED", None
        result = None
        deadline = time.monotonic() + min(900, int(self.config.get("job_timeout_s", 600)))
        if context.deadline_ms is not None:
            deadline = min(deadline, time.monotonic() + context.deadline_ms / 1000 - time.time())
        try:
            # Queue waiting counts toward the deadline and remains cancellable.
            while self.slots.locked():
                await self.checkpoint(job, deadline)
            async with self.slots:
                await self.checkpoint(job, deadline, delay=0)
                if op == "scene_render":
                    gpu = Path(self.config["gpu_lock"]).open("a+")
                    while True:
                        try:
                            fcntl.flock(gpu, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await self.checkpoint(job, deadline)
                self.set_phase(job, "running")
                await events.emit(ToolEvent(type="progress", data={"stage": "executing"}))
                argv = [self.config["isaac_python"], str(Path(__file__).with_name("worker.py")),
                        "--config", str(self.config_path), "--request", str(directory / "request.json")]
                # Do not pass provider routing, proxy or credentials into Isaac.
                env = worker_environment()
                env.update({"PYTHONNOUSERSITE": "1", "CUDA_VISIBLE_DEVICES": str(self.config.get("gpu_index", 0)),
                            JOB_MARKER: job})
                leases = (self.owner.fileno(),) + ((gpu.fileno(),) if gpu else ())
                env["ISAAC_SCENE_LEASE_FDS"] = ",".join(map(str, leases))
                with (directory / "worker.log").open("xb") as log:
                    process = await asyncio.create_subprocess_exec(*argv, cwd=directory, env=env,
                        stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True, pass_fds=leases)
                    dump(directory / "process.json", {"pid": process.pid, "created_at": time.time()})
                    while process.returncode is None:
                        await self.checkpoint(job, deadline)
                    await process.wait()
                await self.checkpoint(job, deadline, delay=0)
                if process.returncode != 0:
                    raise RuntimeError("worker failed; see private job log")
                result = json.loads((directory / "result.json").read_text())
                phase, code = "completed", None
        except asyncio.CancelledError:
            phase, code = "cancelled", None
        except TimeoutError:
            phase, code = "failed", "JOB_DEADLINE_EXCEEDED"
        except Exception:
            import traceback
            (directory / "adapter-error.log").write_text(traceback.format_exc())
        finally:
            cleanup_ok = False
            try:
                # Renderers can start detached helpers in another process group.
                # The per-job marker, not executable names, proves ownership.
                remaining = await processes.terminate()
                cleanup_ok = not remaining
                if cleanup_ok and process is not None:
                    await asyncio.wait_for(process.wait(), 1)
                dump(directory / "cleanup.private.json", {"status": "pass" if cleanup_ok else "fail",
                     "observed": list(processes.observed.values()), "remaining": remaining})
            except Exception:
                import traceback
                (directory / "cleanup-error.log").write_text(traceback.format_exc())
                cleanup_ok = False
            if gpu and cleanup_ok:
                gpu.close()
            if not cleanup_ok:
                self.pending_cleanup[job] = (gpu, processes)
            if job in self.cancelled:
                phase, code, outputs = "cancelled", None, None
            if not cleanup_ok:
                phase, code, outputs = "failed", "PROCESS_CLEANUP_FAILED", None
            if phase == "completed":
                try:
                    outputs = self.publish(job, op, result)
                except Exception:
                    import traceback
                    (directory / "publication-error.log").write_text(traceback.format_exc())
                    self.db.rollback()
                    phase, code, outputs = "failed", "RESULT_PUBLICATION_FAILED", None
            self.set_phase(job, phase, outputs if phase == "completed" else None, code)
            self.cancelled.discard(job)
            event = {"completed": "executor_completed", "failed": "executor_failed", "cancelled": "cancelled"}[phase]
            with contextlib.suppress(Exception):
                await events.emit(ToolEvent(type=event, data={"job_id": job}))

    def publish(self, job, op, result):
        """Commit identities, artifacts and result together, without yielding."""
        directory = self.root / job
        scene_id = result.get("scene_id") or uuid.uuid4().hex
        if op in {"scene_build", "scene_edit", "scene_solve"}:
            request = json.loads((directory / "request.json").read_text())["arguments"]
            self.db.execute("INSERT INTO scenes VALUES (?,?,?,?)", (scene_id, request.get("scene_id"),
                (directory / "layout.json").read_text(), job))
        artifacts = []
        # Neither USD nor detailed dependency/renderer reports are public.
        for name, role in (("image.png", "image"), ("summary.json", "summary")):
            path = directory / name
            if path.is_file():
                content = path.read_bytes()
                item = {"artifact_id": uuid.uuid4().hex, "role": role, "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()}
                self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?)", (item["artifact_id"], job,
                    name, role, item["size_bytes"], item["sha256"]))
                artifacts.append(item)
        outputs = {"scene_id": scene_id, "artifacts": artifacts, "delivery_scope": "review_scene"}
        if op == "scene_validate":
            outputs["checks"] = result["checks"]
        if op == "scene_solve":
            outputs["placement"] = result["placement"]
        Draft202012Validator(output_schema(op)).validate(outputs)
        return outputs

    async def checkpoint(self, job, deadline, delay=.1):
        if job in self.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() >= deadline:
            raise TimeoutError
        await asyncio.sleep(delay)

    async def close(self):
        self.cancelled.update(self.tasks)
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for job, (lease, processes) in list(self.pending_cleanup.items()):
            if not await processes.terminate():
                if lease:
                    lease.close()
                self.pending_cleanup.pop(job)
        if self.pending_cleanup:
            raise RuntimeError("owned processes remain; operator cleanup required")
        self.db.close()
        self.owner.close()


class Operation:
    def __init__(self, service, name):
        self.service, self.name = service, name

    async def query(self, request, context):
        try:
            outputs = self.service.query(self.name, request.arguments)
            Draft202012Validator(output_schema(self.name)).validate(outputs)
            return ToolResult(status="succeeded", outputs=outputs)
        except Exception:
            return ToolResult(status="failed", error=error("INVALID_ARGUMENTS"))

    async def start(self, request, context, events):
        return await self.service.start(self.name, request, context, events)

    async def cancel(self, key, reason=None):
        row = self.service.row(key)
        if row["phase"] not in {"accepted", "running", "stopping"}:
            return ToolControlResponse(command="cancel", status="terminal")
        self.service.cancelled.add(row["id"])
        self.service.set_phase(row["id"], "stopping")
        return ToolControlResponse(command="cancel", status="accepted")

    async def status(self, key):
        row = self.service.row(key)
        return ToolExecutionStatus(phase=row["phase"], error=error(row["error"]) if row["error"] else None)

    async def result(self, key):
        row = self.service.row(key)
        if row["phase"] in {"accepted", "running", "stopping"}:
            return ToolResultResponse(status="pending")
        status = "succeeded" if row["phase"] == "completed" else row["phase"]
        return ToolResultResponse(status="available", result=ToolResult(status=status,
            outputs=json.loads(row["output"]) if row["output"] else {}, error=error(row["error"]) if row["error"] else None))
