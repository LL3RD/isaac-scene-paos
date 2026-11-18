"""Source audit, pinned dependency setup, tests and portable source packaging.

Only fetch contacts the network. No command starts Isaac, publishes a resource,
changes Git identity, commits, pushes, or installs system dependencies.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAMS = {"gateway": "gateway_and_bundled_forge_tool", "paos": "phyagentos_core"}


def run(*args, cwd=ROOT, **kwargs):
    return subprocess.run([str(x) for x in args], cwd=cwd, check=True, **kwargs)


def git(*args, cwd=ROOT):
    return run("git", *args, cwd=cwd, capture_output=True, text=True).stdout.strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_core_auditor():
    sys.path.insert(0, str(ROOT / "core/scripts"))
    spec = importlib.util.spec_from_file_location("core_package_release", ROOT / "core/scripts/package_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_files():
    """Include staged AND unstaged source, but never ignored operator files."""
    result = run("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", capture_output=True)
    return sorted(set(name for name in result.stdout.decode().split("\0") if name))


def audit():
    core = load_core_auditor().audit(ROOT / "core")
    expected = json.loads((ROOT / "core.lock.json").read_text())
    if core != expected:
        raise ValueError("core snapshot differs from core.lock.json; review the update explicitly")
    patterns = load_core_auditor().PATTERNS
    allowed_roots = {"adapter", "core", "scripts", "tests", "docs", ".github"}
    allowed_top = {".gitignore", "LICENSE", "NOTICE", "README.md", "README_zh.md", "SKILL.md",
                   "VERSION", "requirements.lock", "upstream.lock.json", "core.lock.json", "deployment.example.json"}
    files = []
    for name in source_files():
        rel = Path(name)
        if (len(rel.parts) == 1 and name not in allowed_top) or (len(rel.parts) > 1 and rel.parts[0] not in allowed_roots):
            raise ValueError(f"Unexpected source path: {name}")
        if (len(rel.parts) > 1 and rel.parts[0] != "core"
                and rel.suffix not in {".py", ".md", ".json", ".yml", ".yaml"}):
            raise ValueError(f"Unexpected source file type: {name}")
        path = ROOT / rel
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError(f"Not a regular bounded source file: {name}")
        data = path.read_bytes()
        text = data.decode("utf-8")
        for code, pattern in patterns.items():
            if pattern.search(text):
                raise ValueError(f"Source scan: {code} in {name}")
        files.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return {"status": "pass", "core_files": len(core["files"]), "source_files": len(files), "files": files,
            "scope": "working source inventory and targeted privacy scan, not a security or Git-history audit"}


def verify_checkout(name, path):
    lock = json.loads((ROOT / "upstream.lock.json").read_text())[UPSTREAMS[name]]
    path = Path(path).resolve()
    if not (path / ".git").exists():
        expected = archive_files(name)
        actual = {p.relative_to(path).as_posix(): sha(p) for p in path.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        if actual != {n: hashlib.sha256(data).hexdigest() for n, data in expected.items()}:
            raise ValueError(f"Modified {name} archive checkout")
        if any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError(f"Unexpected symlink in {name}")
        return lock
    if git("rev-parse", "HEAD", cwd=path) != lock["commit"]:
        raise ValueError(f"Wrong {name} commit")
    if git("status", "--porcelain", "--untracked-files=all", cwd=path):
        raise ValueError(f"Dirty {name} checkout")
    return lock


def archive_files(name):
    lock = json.loads((ROOT / "upstream.lock.json").read_text())[UPSTREAMS[name]]
    archive = ROOT / ".deps" / (name + ".tar.gz")
    if sha(archive) != lock["fetch_archive_sha256"]:
        raise ValueError(f"Wrong {name} source archive digest")
    files = {}
    with tarfile.open(archive) as tar:
        if tar.pax_headers.get("comment") != lock["commit"]:
            raise ValueError(f"Wrong {name} source archive commit")
        roots = set()
        for member in tar.getmembers():
            rel = Path(member.name)
            roots.add(rel.parts[0])
            if rel.is_absolute() or ".." in rel.parts or not (member.isdir() or member.isfile()):
                raise ValueError("Upstream archive must contain only relative regular files")
            if member.isfile():
                key = Path(*rel.parts[1:]).as_posix()
                if key in files or len(rel.parts) < 2:
                    raise ValueError("Unexpected upstream archive layout")
                files[key] = tar.extractfile(member).read()
        if len(roots) != 1:
            raise ValueError("Upstream archive needs one root directory")
    return files


def fetch(names, method="archive"):
    locks = json.loads((ROOT / "upstream.lock.json").read_text())
    for name in names:
        target = ROOT / ".deps" / name
        lock = locks[UPSTREAMS[name]]
        if method == "archive":
            archive = target.parent / (name + ".tar.gz")
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                owner_repo = lock["repository"].split("github.com/", 1)[1]
                url = f"https://codeload.github.com/{owner_repo}/tar.gz/{lock['commit']}"
                # Buffer first so an interrupted download never becomes a cached archive.
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read()
                if hashlib.sha256(data).hexdigest() != lock["fetch_archive_sha256"]:
                    raise ValueError(f"Downloaded {name} archive digest mismatch")
                with archive.open("xb") as stream:
                    stream.write(data)
            files = archive_files(name)
            if not target.exists():
                target.mkdir()
                for filename, data in files.items():
                    destination = target / filename
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as stream:
                        stream.write(data)
            verify_checkout(name, target)
            print(f"{name}: commit, archive SHA256 and extracted file hashes verified")
            continue
        if not target.exists():
            target.mkdir(parents=True)
            run("git", "init", "--quiet", target)
            run("git", "-C", target, "remote", "add", "origin", lock["repository"])
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=target,
                              capture_output=True, text=True)
        if head.returncode:
            # A failed initial network fetch leaves an empty initialized checkout.
            # Retry only that state; never reset/overwrite a user's working tree.
            if git("remote", "get-url", "origin", cwd=target) != lock["repository"] or git("status", "--porcelain", cwd=target):
                raise ValueError(f"Unexpected incomplete checkout: {name}")
            run("git", "-C", target, "fetch", "--depth=1", "origin", lock["commit"], timeout=90)
            run("git", "-C", target, "checkout", "--detach", "FETCH_HEAD")
        verify_checkout(name, target)
        print(f"{name}: pinned source verified")


def export_source(output):
    report = audit()
    # Fixed ZIP metadata makes repeated exports of identical sources byte-identical.
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        payloads = [(r["path"], (ROOT / r["path"]).read_bytes()) for r in report["files"]]
        for row, (_, data) in zip(report["files"], payloads):
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Source changed during export")
        payloads.append(("source-manifest.json", (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()))
        for name, data in payloads:
            info = zipfile.ZipInfo("isaac-scene-paos/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return {"status": "pass", "sha256": sha(output), "bytes": output.stat().st_size,
            "files": report["source_files"], "git_metadata_included": False}


def tests(report_directory=None):
    import os
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(ROOT / ".deps/gateway/src")}
    verify_checkout("gateway", ROOT / ".deps/gateway")
    suites = (("repository", ROOT, ["discover", "-s", "tests", "-v"]),
              ("core", ROOT, ["discover", "-s", "core/tests", "-v"]),
              ("adapter", ROOT / "adapter", ["test_adapter", "-v"]))
    if report_directory:
        report_directory = Path(report_directory).resolve()
        report_directory.mkdir(parents=True, exist_ok=False)
    reports = {}
    for name, cwd, arguments in suites:
        result = subprocess.run([sys.executable, "-m", "unittest", *arguments], cwd=cwd, env=env,
                                capture_output=True, text=True)
        log = result.stdout + result.stderr
        count = re.search(r"Ran (\d+) tests? in", log)
        skipped = re.search(r"skipped=(\d+)", log)
        reports[name] = {"status": "pass" if result.returncode == 0 and count else "fail",
                         "run": int(count[1]) if count else 0,
                         "skipped": int(skipped[1]) if skipped else 0}
        if report_directory:
            (report_directory / (name + ".log")).write_text(log)
        if result.returncode:
            print(log, file=sys.stderr)
        print(name + ": " + json.dumps(reports[name]), flush=True)
    summary = {"status": "pass" if all(r["status"] == "pass" for r in reports.values()) else "fail", "suites": reports}
    if report_directory:
        (report_directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if summary["status"] != "pass":
        raise RuntimeError("Source test failure; inspect the suite log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit")
    fetcher = commands.add_parser("fetch")
    fetcher.add_argument("names", nargs="+", choices=sorted(UPSTREAMS))
    fetcher.add_argument("--method", choices=("archive", "git"), default="archive")
    tester = commands.add_parser("test")
    tester.add_argument("--report", type=Path, help="new private directory for logs and counts")
    exporter = commands.add_parser("export")
    exporter.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "audit":
        report = audit()
        print(json.dumps({k: v for k, v in report.items() if k != "files"}))
    elif args.command == "fetch":
        fetch(args.names, args.method)
    elif args.command == "test":
        tests(args.report)
    else:
        print(json.dumps(export_source(args.output)))


if __name__ == "__main__":
    main()
