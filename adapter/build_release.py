"""Build Linux node artifacts and an official manifest-v2 Skill; no USD assets."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile

from contracts import ENDPOINT, tool_specs

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
from repository import audit, git, verify_checkout, export_source

VERSION = (REPOSITORY / "VERSION").read_text().strip()


def provenance(repository_url, tag):
    """Publishable artifacts must refer to an actual clean, tagged source commit."""
    from urllib.parse import urlsplit
    import re
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a public HTTPS source repository URL without credentials")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?", VERSION):
        raise ValueError("Set a release VERSION; development snapshots are not publishable")
    if tag != "v" + VERSION:
        raise ValueError("Tag must be v<VERSION>")
    commit = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Commit the reviewed sources before building")
    if git("rev-parse", tag + "^{commit}") != commit:
        raise ValueError("Release tag must resolve to the source HEAD")
    return {"repository": repository_url, "commit": commit, "tag": tag}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def portable_member(info):
    """Do not publish the builder's Unix account or host timestamps in tar headers."""
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
    info.pax_headers = {}
    return info


def write_skill(bundle, source, nodes, version, resolver="local"):
    """Generate the same profile for local review and exact Registry locks."""
    if resolver not in {"local", "registry"} or set(nodes) != {"provider", "gateway"}:
        raise ValueError("Expected local/registry resolver and both Node locks")
    bundle.mkdir(parents=True, exist_ok=False)
    profile = bundle / "profiles/local-review"
    profile.mkdir(parents=True)
    for name in ("SKILL.md", "LICENSE", "NOTICE", "deployment.example.json"):
        shutil.copy2(source / name, bundle / name)
    write(profile / "gateway.yaml", {"host": "127.0.0.1", "port": 19043, "joint_order": [],
        "tools": {"enabled": True, "invoke_timeout_ms": 900000, "lease_ttl_ms": 15000,
            "providers": [{"endpoint_id": ENDPOINT, "input_id": "provider/to_gateway", "output_id": "to_provider"}],
            "specs": tool_specs()}})
    write(profile / "zenoh.json", {"listen": {"endpoints": ["tcp/127.0.0.1:0"]},
        "scouting": {"multicast": {"enabled": False}, "gossip": {"enabled": False}}})
    write(profile / "dataflow.yaml", {"nodes": [
        {"id": "gateway", "path": "${FORGE_RUNTIME_BIN}/" + nodes["gateway"]["entrypoint"],
         "args": "--config gateway.yaml", "inputs": {"provider/to_gateway": {"source": "provider/to_gateway", "queue_size": 128}},
         "outputs": ["to_provider"]},
        {"id": "provider", "path": "${FORGE_RUNTIME_BIN}/" + nodes["provider"]["entrypoint"],
         "inputs": {"from_gateway": {"source": "gateway/to_provider", "queue_size": 128},
                       "heartbeat": "dora/timer/millis/2000"}, "outputs": ["to_gateway"]}]})
    write(bundle / "skill.yaml", {"manifest_version": 2, "name": "isaac-scene-paos", "version": version,
        "description": "Isaac scene review provider with immutable revisions; external Agent planning.",
        "skill_document": "SKILL.md", "gateway_url": "http://127.0.0.1:19043",
        "required_tools": [s["tool_id"] for s in tool_specs()],
        "profiles": {"local-review": {"dataflow": "profiles/local-review/dataflow.yaml", "startup_timeout_s": 60,
            "required_binaries": [n["entrypoint"] for n in nodes.values()],
            "required_assets": ["profiles/local-review/gateway.yaml", "profiles/local-review/zenoh.json"],
            "required_environment": ["ISAAC_SCENE_DEPLOYMENT", "ZENOH_CONFIG"], "environment": {}}},
        "artifacts": {"resolver": resolver, "nodes": nodes}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-source", type=Path, required=True)
    parser.add_argument("--core-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--candidate", action="store_true",
                        help="non-publishable development build; records source snapshot, no tag needed")
    args = parser.parse_args()
    if (platform.system(), platform.machine()) != ("Linux", "x86_64"):
        parser.error("build on the target Linux x86_64 host")
    inventory = audit()
    if args.candidate:
        if args.tag:
            parser.error("candidate builds cannot claim a release tag")
        source_identity = {"repository": args.repository_url, "commit": git("rev-parse", "HEAD"),
                           "tag": None, "dirty": bool(git("status", "--porcelain"))}
    else:
        source_identity = provenance(args.repository_url, args.tag)
    verify_checkout("gateway", args.gateway_source)
    verify_checkout("paos", args.core_source)
    locked_packages = dict(line.split(" --hash=", 1)[0].split("==", 1) for line in
        (REPOSITORY / "requirements.lock").read_text().splitlines() if line and not line.startswith("#"))
    for name, version in locked_packages.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"Build dependency differs from lock: {name}")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    gateway = args.gateway_source.resolve()
    core = args.core_source.resolve()
    snapshot = root / "source"
    snapshot.mkdir()
    for row in inventory["files"]:
        name = row["path"]
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY / name, target)
        if digest(target) != row["sha256"]:
            raise ValueError("Source changed during build snapshot")
    export_source(root / f"isaac-scene-paos-{VERSION}-source.zip")
    source = snapshot / "adapter"
    # Build metadata is generated, not hand-maintained duplicate dependency lists.
    packages = sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions())
    notices = root / "notices"
    notices.mkdir()
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(snapshot / name, notices / name)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")
    for dist in importlib.metadata.distributions():
        for entry in dist.files or ():
            parts = Path(entry).parts
            if (Path(entry).name.lower().startswith(("license", "copying", "notice"))
                    and "__pycache__" not in parts and Path(entry).suffix not in {".py", ".pyc", ".so"}):
                original = Path(dist.locate_file(entry))
                if original.is_file():
                    target = notices / dist.metadata["Name"] / Path(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
    for name, checkout in (("gateway", gateway), ("core", core)):
        for path in checkout.glob("LICENSE*"):
            shutil.copy2(path, notices / f"{name}-{path.name}")
    write(notices / "packages.json", dict(packages))
    env = {**os.environ, "PYTHONPATH": str(gateway / "src"), "PYTHONHASHSEED": "0"}
    nodes = {}
    targets = (
        ("provider", "isaac_scene_provider", source / "node.py",
         ["--add-data", f"{source / 'worker.py'}:.", "--add-data", f"{source / 'catalog.py'}:.",
          "--add-data", f"{source / 'contracts.py'}:.",
          "--add-data", f"{snapshot / 'core'}:scene_core", "--hidden-import", "smoke",
          "--collect-submodules", "forge_tool"]),
        ("gateway", "isaac_scene_gateway", gateway / "src/forge_gateway/__main__.py",
         ["--add-data", f"{gateway / 'src/forge_gateway/resources'}:forge_gateway/resources",
          "--collect-submodules", "forge_gateway", "--collect-submodules", "uvicorn",
          "--collect-submodules", "fastapi", "--hidden-import", "cv2"]),
    )
    for node_id, name, entry, extra in targets:
        command = [sys.executable, "-m", "PyInstaller", "--onedir", "--noupx",
                   "--name", name, "--distpath", str(root / "binaries"),
                   "--workpath", str(root / "build" / name), "--specpath", str(root / "build"),
                   "--paths", str(gateway / "src"), "--paths", str(source),
                   "--paths", str(snapshot / "scripts"),
                   "--hidden-import", "dora", "--hidden-import", "pyarrow",
                   "--collect-submodules", "forge_msgs", *extra, str(entry)]
        with (root / f"{name}.build.log").open("x") as log:
            subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        tree = root / "binaries" / name
        shutil.copytree(notices, tree / "THIRD_PARTY_NOTICES")
        subprocess.run([str(tree / name), "--help"], check=True, stdout=subprocess.DEVNULL)
        archive = root / f"{name}-{VERSION}-linux-x86_64.tar.gz"
        # PyInstaller uses shared-library symlinks; publish regular files only.
        with tarfile.open(archive, "x:gz", dereference=True) as tar:
            tar.add(tree, arcname=name, filter=portable_member)
        nodes[node_id] = {"artifact_id": f"local.{name}.{VERSION}.{digest(archive)[:12]}",
            "version": VERSION, "platform": "linux", "arch": "x86_64",
            "artifact_type": "directory_tar_gz", "entrypoint": name, "sha256": digest(archive)}
    bundle = root / "isaac-scene-paos"
    write_skill(bundle, snapshot, nodes, VERSION)
    package_env = {**os.environ, "PYTHONPATH": str(core)}
    subprocess.run([sys.executable, str(core / "scripts/package_skill.py"), str(bundle),
                    "--output-dir", str(root / "skill-archive")], env=package_env, check=True)
    write(root / "release.json", {"version": VERSION, "platform": platform.platform(),
        "python": platform.python_version(), "packages": dict(packages),
        "source": source_identity, "candidate": args.candidate,
        "build": {"entrypoint": "adapter/build_release.py", "mode": "manual",
            "reproduction_command": ["python", "adapter/build_release.py", "--gateway-source", ".deps/gateway",
                "--core-source", ".deps/paos", "--output", ".local/build-01",
                "--repository-url", args.repository_url,
                *(["--candidate"] if args.candidate else ["--tag", args.tag])],
            "source_test_workflow": {"path": ".github/workflows/checks.yml",
                "revision": source_identity["commit"], "execution": "not asserted by builder"}},
        "sources": {r["path"]: r["sha256"] for r in inventory["files"]},
        "upstream": json.loads((snapshot / "upstream.lock.json").read_text()), "nodes": nodes,
        "archives": {p.name: {"sha256": digest(p), "bytes": p.stat().st_size}
                     for p in [*root.glob("*.tar.gz"), *root.glob("*.zip"), *root.glob("skill-archive/*.tar.gz")]},
        "scope": "local install candidate; no registry publication or USD assets"})


if __name__ == "__main__":
    main()
