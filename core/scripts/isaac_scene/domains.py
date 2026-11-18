from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_domains(skill_root: str | Path) -> list[dict[str, Any]]:
    root = Path(skill_root) / "domains"
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for manifest_path in sorted(root.glob("*/domain.json")):
        manifest = _read_object(manifest_path)
        if not manifest.get("name"):
            continue
        manifest["manifest_path"] = str(manifest_path)
        ontology_path = manifest_path.with_name("ontology.json")
        manifest["ontology_path"] = str(ontology_path) if ontology_path.is_file() else None
        rows.append(manifest)
    return rows


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return phrase in text.lower()


def route_domain(skill_root: str | Path, prompt: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for domain in discover_domains(skill_root):
        if domain.get("name") == "generic_scene":
            continue
        triggers = domain.get("triggers") if isinstance(domain.get("triggers"), dict) else {}
        matches: list[str] = []
        for values in triggers.values():
            if not isinstance(values, list):
                continue
            matches.extend(str(item) for item in values if _contains(prompt, str(item)))
        if matches:
            candidates.append({
                "domain": domain["name"],
                "score": min(1.0, 0.45 + 0.12 * len(set(matches))),
                "matched_triggers": sorted(set(matches), key=lambda item: (-len(item), item)),
                "manifest_path": domain["manifest_path"],
            })
    candidates.sort(key=lambda item: (item["score"], len(item["matched_triggers"])), reverse=True)
    if not candidates:
        return {"domain": "generic_scene", "confidence": 0.25, "candidates": [], "reason": "no specialized trigger matched"}
    best = candidates[0]
    return {"domain": best["domain"], "confidence": best["score"], "candidates": candidates, "reason": "matched domain triggers"}


def ontology_synonyms(skill_root: str | Path, domains: list[str] | None = None) -> dict[str, list[str]]:
    wanted = set(domains or [])
    synonyms: dict[str, list[str]] = {}
    for domain in discover_domains(skill_root):
        if wanted and domain.get("name") not in wanted:
            continue
        ontology_path = domain.get("ontology_path")
        if not ontology_path:
            continue
        raw = _read_object(Path(str(ontology_path))).get("synonyms")
        if not isinstance(raw, dict):
            continue
        for phrase, values in raw.items():
            if isinstance(values, list):
                synonyms.setdefault(str(phrase), []).extend(str(item) for item in values)
    return {key: list(dict.fromkeys(values)) for key, values in synonyms.items()}


def expand_query(skill_root: str | Path, query: str, domains: list[str] | None = None) -> dict[str, Any]:
    additions: list[str] = []
    matched: dict[str, list[str]] = {}
    for phrase, values in ontology_synonyms(skill_root, domains).items():
        if phrase.lower() in query.lower():
            matched[phrase] = values
            additions.extend(values)
    expanded = " ".join([query, *dict.fromkeys(additions)]).strip()
    return {"original": query, "expanded": expanded, "matched_synonyms": matched}


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower().replace("_", " ").replace("-", " ").replace("/", " ")
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text)
