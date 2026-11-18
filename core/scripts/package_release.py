#!/usr/bin/env python3
"""Build an allowlisted, reproducible source ZIP; never package the parent directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

from isaac_scene.output_policy import new_output

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = 'config/release_files.txt'
IGNORED = {'__pycache__', '.pytest_cache', '.ruff_cache', '.git', '.state', 'runs', 'runtime', 'dist'}
PATTERNS = {
    'machine_home': re.compile(r'(?<![A-Za-z0-9_./])/' + r'(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private_mount': re.compile(r'/mnt/' + r'nfs\d+|/data/' + r'hjj(?:/|\b)'),
    'private_network': re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'common_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{32,})\b'),
}


def audit(root=ROOT):
    root = Path(root).resolve()
    lines = (root / ALLOWLIST).read_text().splitlines()
    names = [line for line in lines if line and not line.startswith('#')]
    if len(names) != len(set(names)) or ALLOWLIST not in names:
        raise ValueError('Invalid release file allowlist')
    actual = set()
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if any(part in IGNORED for part in rel.parts) or path.name == '.DS_Store':
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink is not a distributable source: {rel}')
        if path.is_file():
            actual.add(rel.as_posix())
    # ZIPs include a generated manifest in addition to source files. Accept it
    # only when it exactly matches the source inventory computed below.
    actual.discard('release-manifest.json')
    if actual != set(names):
        raise ValueError(f'Allowlist mismatch: extra={sorted(actual-set(names))}, missing={sorted(set(names)-actual)}')
    files = []
    for name in sorted(names):
        rel = Path(name)
        path = root / rel
        if rel.is_absolute() or '..' in rel.parts or path.resolve() != path.absolute():
            raise ValueError('Release allowlist must contain relative regular files')
        if path.stat().st_size > 5_000_000:
            raise ValueError(f'Unexpected large source file: {name}')
        data = path.read_bytes()
        text = data.decode('utf-8')  # No binary asset, cache or hidden environment payload.
        for code, pattern in PATTERNS.items():
            if pattern.search(text):
                raise ValueError(f'Distribution scan failed: {code} in {name}')
        files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)})
    report = {'schema_version': 'isaac-scene-source-release/v1', 'status': 'pass',
              'scope': 'allowlisted source only; not runtime isolation or history audit', 'files': files}
    generated = root / 'release-manifest.json'
    if generated.exists() and json.loads(generated.read_text()) != report:
        raise ValueError('Generated release manifest no longer matches the source')
    return report


def package(output, root=ROOT):
    root = Path(root).resolve()
    destination = new_output(output)
    if root == destination or root in destination.parents:
        raise ValueError('Write the archive outside the skill source directory')
    manifest = audit(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        payloads = []
        for row in manifest['files']:
            data = (root / row['path']).read_bytes()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ValueError('Source changed during packaging; discard partial archive and rebuild to a new path')
            payloads.append((row['path'], data))
        payloads.append(('release-manifest.json', (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()))
        for name, data in payloads:
            info = zipfile.ZipInfo('isaac-scene-skill-lite/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return {'status': 'pass', 'file_count': len(manifest['files']),
            'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(), 'size_bytes': destination.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        result = audit()
        print(json.dumps({'status': result['status'], 'file_count': len(result['files'])}))
    elif args.output:
        print(json.dumps(package(args.output), indent=2))
    else:
        parser.error('Use --check or --output')


if __name__ == '__main__':
    main()
