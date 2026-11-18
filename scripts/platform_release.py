"""Prepare registration files and Registry-locked Skills. Never uploads or publishes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapter"))
from build_release import digest, write, write_skill
from repository import verify_checkout

LOCK_FIELDS = ("artifact_id", "version", "platform", "arch", "artifact_type", "entrypoint", "sha256")


def https_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("Use HTTPS URLs without credentials or fragments")
    return value


def github_source(value):
    https_url(value)
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Source must be a GitHub repository root URL")
    return value


def read_build(directory):
    """Work only from the immutable native build snapshot, never the current tree."""
    directory = Path(directory).resolve()
    release = json.loads((directory / "release.json").read_text())
    if release.get("candidate", True) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", release["version"]):
        raise ValueError("Development/candidate builds cannot prepare platform releases")
    source = release["source"]
    github_source(source["repository"])
    if source.get("tag") != "v" + release["version"] or not re.fullmatch(r"[a-f0-9]{40}", source["commit"]):
        raise ValueError("Build must identify a tagged source commit")
    if source.get("dirty") or set(release["nodes"]) != {"provider", "gateway"}:
        raise ValueError("Expected clean source and both Node artifacts")
    for name, expected in release["sources"].items():
        path = directory / "source" / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory / "source") or digest(path) != expected:
            raise ValueError("Build source snapshot differs: " + name)
    for node in release["nodes"].values():
        archive = node_archive(directory, node)
        record = release["archives"][archive.name]
        if digest(archive) != node["sha256"] or record["sha256"] != node["sha256"] or archive.stat().st_size != record["bytes"]:
            raise ValueError("Node archive differs from the recorded build")
    return directory, release


def node_archive(directory, node):
    for field in ("entrypoint", "version", "platform", "arch"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node[field]) or node[field] in {".", ".."}:
            raise ValueError("Invalid Node identity")
    return directory / f"{node['entrypoint']}-{node['version']}-{node['platform']}-{node['arch']}.tar.gz"


def node_inputs(build, output, ids, repositories):
    directory, release = read_build(build)
    if set(ids) != {"provider", "gateway"} or len(set(ids.values())) != 2:
        raise ValueError("Assign two distinct Node artifact IDs")
    rows = {}
    for key, kind in (("provider", "operator"), ("gateway", "adapter")):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ids[key]) or ids[key].startswith("local."):
            raise ValueError("Assign non-local artifact IDs agreed with maintainers")
        if not re.fullmatch(kind + r"-[A-Za-z0-9][A-Za-z0-9_.-]*", repositories[key]):
            raise ValueError("Publication repository must use the Node type prefix")
        lock = release["nodes"][key]
        rows[key] = {"kind": "node", "name": lock["entrypoint"], "version": lock["version"],
            "bundle": os.path.relpath(node_archive(directory, lock), Path(output).resolve()),
            "metadata": {**{k: lock[k] for k in LOCK_FIELDS if k != "sha256"},
                "artifact_id": ids[key], "node_type": kind, "repository": repositories[key],
                "github_url": release["source"]["repository"]}}
    Path(output).mkdir(parents=True, exist_ok=False)
    for key, row in rows.items():
        write(Path(output) / (key + ".json"), row)
    write(Path(output) / "node-ids.json", ids)
    # Build recipe lives in this project; Gateway's upstream identity stays explicit.
    write(Path(output) / "provenance.json", {"source": release["source"], "upstream": release["upstream"],
        "nodes": release["nodes"], "status": "prepared_only_not_registered"})


def public_node(base_url, artifact_id):
    https_url(base_url)
    if urlsplit(base_url).query:
        raise ValueError("Registry base URL must not have a query")
    with urlopen(base_url.rstrip("/") + "/v1/forge-nodes/" + quote(artifact_id, safe=""), timeout=30) as response:
        https_url(response.geturl())
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError("Registry metadata exceeds expected size")
    return json.loads(data)


def registry_lock(value, expected, artifact_id):
    """Accept flat or nested Registry records; reject missing/conflicting fields."""
    if not isinstance(value, dict):
        raise ValueError("Registry response must be an object")
    sources = [value] + [value[k] for k in ("metadata", "artifact") if isinstance(value.get(k), dict)]
    target = {k: expected[k] for k in LOCK_FIELDS}
    target["artifact_id"] = artifact_id
    for field, wanted in target.items():
        found = [s[field] for s in sources if field in s]
        if not found or any(v != wanted for v in found):
            raise ValueError("Registry Node mismatch: " + field)
    if any(s.get("status", "published") != "published" for s in sources):
        raise ValueError("Registry Node is not published")
    return target


def verify_download(value, expected_sha, expected_bytes):
    source = value.get("artifact", value)
    url = https_url(source.get("download_url", source.get("url", "")))
    sha, size = hashlib.sha256(), 0
    with urlopen(url, timeout=60) as stream:
        https_url(stream.geturl())
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > expected_bytes:
                raise ValueError("Registry download is larger than the built artifact")
            sha.update(block)
    if size != expected_bytes or sha.hexdigest() != expected_sha:
        raise ValueError("Registry download does not match the built artifact")


def registry_bundle(build, output, ids, registry_url, core_source):
    directory, release = read_build(build)
    if set(ids) != {"provider", "gateway"} or len(set(ids.values())) != 2:
        raise ValueError("Expected distinct provider and gateway Registry IDs")
    verify_checkout("paos", core_source)
    nodes = {}
    for key, expected in release["nodes"].items():
        if ids[key].startswith("local."):
            raise ValueError("Local artifact IDs are not Registry dependencies")
        record = public_node(registry_url, ids[key])
        nodes[key] = registry_lock(record, expected, ids[key])
        verify_download(record, expected["sha256"], node_archive(directory, expected).stat().st_size)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_skill(output / "source", directory / "source", nodes, release["version"], "registry")
    subprocess.run([sys.executable, str(Path(core_source).resolve() / "scripts/package_skill.py"),
                    str(output / "source"), "--output-dir", str(output / "artifacts")], check=True)
    bundle = output / "artifacts" / f"isaac-scene-paos-{release['version']}.tar.gz"
    write(output / "release-proof.json", {"source": release["source"], "nodes": nodes,
        "bundle_sha256": digest(bundle), "bundle_bytes": bundle.stat().st_size,
        "verification": "public_registry_metadata_and_download_sha", "evidence": "not_recorded",
        "skill_published": False})
    return bundle


def evidence_input(bundle, video, observed_sha, context, output):
    """Bind an operator's real-run record; never pretend to assess video success."""
    bundle, video, output = Path(bundle).resolve(), Path(video).resolve(), Path(output).resolve()
    if digest(bundle) != observed_sha:
        raise ValueError("Bundle changed since the recorded run")
    if not video.is_file() or video.stat().st_size == 0:
        raise ValueError("Supply the actual nonempty successful-run video")
    with tarfile.open(bundle) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or len(names) > 10_000 or any(not m.isfile() for m in members):
            raise ValueError("Expected the generated file-only Skill archive")
        inventory = json.load(archive.extractfile("archive-manifest.json"))["files"]
        records = {r["path"]: r for r in inventory}
        if len(records) != len(inventory) or set(records) != set(names) - {"archive-manifest.json"}:
            raise ValueError("Skill inventory does not cover the exact archive")
        for member in members:
            if member.name == "archive-manifest.json":
                continue
            if member.size != records[member.name]["size"]:
                raise ValueError("Skill inventory size mismatch")
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != records[member.name]["sha256"]:
                    raise ValueError("Skill inventory digest mismatch")
        manifest = json.load(archive.extractfile("skill.yaml"))
    if manifest["artifacts"]["resolver"] != "registry" or context["profile"] not in manifest["profiles"]:
        raise ValueError("Evidence requires this Registry bundle's actual profile")
    if not all(isinstance(v, str) and v.strip() for v in context.values()):
        raise ValueError("Fill in the actual run context")
    output.mkdir(parents=True, exist_ok=False)
    write(output / "skill.json", {"kind": "skill", "name": manifest["name"], "version": manifest["version"],
        "bundle": os.path.relpath(bundle, output), "metadata": {
            "description": manifest["description"], "forge_version": 1, "tags": ["simulation"],
            "profile_labels": {context["profile"]: "Local Isaac scene review"}},
        "evidence": [{"path": os.path.relpath(video, output), **context}]})
    write(output / "evidence-record.json", {"bundle_sha256": observed_sha, "video_sha256": digest(video),
        "context": context, "success_assessment": "operator_attested_requires_platform_review",
        "video_decode_check": "not_run", "uploaded": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    nodes = commands.add_parser("nodes", help="prepare Node JSON inputs; does not register")
    nodes.add_argument("--build", required=True, type=Path)
    nodes.add_argument("--output", required=True, type=Path)
    for key in ("provider", "gateway"):
        nodes.add_argument("--" + key + "-id", required=True)
        nodes.add_argument("--" + key + "-repository", required=True)
    bundle = commands.add_parser("bundle", help="GET Registry metadata/downloads; create locked Skill")
    for key in ("build", "output", "node-ids", "core-source"):
        bundle.add_argument("--" + key, required=True, type=Path)
    bundle.add_argument("--registry-url", required=True)
    evidence = commands.add_parser("evidence", help="prepare Skill JSON from an actual run and video")
    for key in ("bundle", "video", "output"):
        evidence.add_argument("--" + key, required=True, type=Path)
    evidence.add_argument("--observed-bundle-sha256", required=True)
    for key in ("profile", "core-version", "environment", "task-goal", "success-description"):
        evidence.add_argument("--" + key, required=True)
    args = parser.parse_args()
    if args.command == "nodes":
        node_inputs(args.build, args.output, {k: getattr(args, k + "_id") for k in ("provider", "gateway")},
                    {k: getattr(args, k + "_repository") for k in ("provider", "gateway")})
    elif args.command == "bundle":
        print(registry_bundle(args.build, args.output, json.loads(args.node_ids.read_text()),
                              args.registry_url, args.core_source))
    else:
        evidence_input(args.bundle, args.video, args.observed_bundle_sha256,
            {k: getattr(args, k) for k in ("profile", "core_version", "environment", "task_goal", "success_description")},
            args.output)


if __name__ == "__main__":
    main()
