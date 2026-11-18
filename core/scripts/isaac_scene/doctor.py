from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .domains import discover_domains


def inspect(skill_root: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    root = Path(skill_root)
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, summary: str, **details: Any) -> None:
        row = {"name": name, "status": status, "summary": summary}
        if details:
            row["details"] = details
        checks.append(row)

    add("skill_root", "pass" if root.is_dir() else "fail", str(root))
    domains = discover_domains(root)
    add("domains", "pass" if domains else "fail", f"{len(domains)} domain packs discovered", domains=[item.get("name") for item in domains])
    capability_path = root / "config" / "capabilities.json"
    try:
        capabilities = json.loads(capability_path.read_text(encoding="utf-8"))
        add("capabilities", "pass", f"{len(capabilities.get('capabilities', []))} capabilities declared")
    except Exception as exc:
        add("capabilities", "fail", f"{type(exc).__name__}: {exc}")

    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    configured = config.get("deployment_configured", bool(config.get("libraries")))
    add("deployment", "pass" if configured else "pending",
        "explicit local deployment" if configured else "configure a runtime and local asset libraries before generation")
    python_spec = runtime.get("python") if isinstance(runtime.get("python"), dict) else {}
    python_path = Path(str(python_spec.get("executable") or sys.executable))
    add("isaac_python", "pass" if python_path.exists() else "pending", str(python_path))
    remote_only_context = False  # Diagnostics are local; never probe network endpoints.

    libraries = config.get("libraries") if isinstance(config.get("libraries"), list) else []
    library_names = {str(lib.get("name")) for lib in libraries if isinstance(lib, dict) and lib.get("name")}
    unknown_domain_libraries = {
        str(domain.get("name")): sorted(set(str(item) for item in domain.get("search_libraries", []) if str(item) not in library_names))
        for domain in domains
        if isinstance(domain.get("search_libraries"), list)
        and any(str(item) not in library_names for item in domain.get("search_libraries", []))
    }
    add(
        "domain_library_contract",
        "warn" if unknown_domain_libraries else "pass",
        "all domain libraries are configured" if not unknown_domain_libraries else "domain packs reference unknown libraries",
        unknown_libraries=unknown_domain_libraries,
    )
    library_rows = []
    required_missing = []
    for lib in libraries:
        if not isinstance(lib, dict):
            continue
        path = Path(str(lib.get("index_path") or lib.get("root") or ""))
        exists = path.exists()
        row = {"name": lib.get("name"), "path": str(path), "exists": exists, "scanner": lib.get("scanner")}
        library_rows.append(row)
        if lib.get("required") and not exists:
            required_missing.append(str(lib.get("name")))
    library_status = "pending" if required_missing and remote_only_context else ("fail" if required_missing else "pass")
    summary_suffix = "; verify on remote host" if library_status == "pending" else ""
    add("asset_libraries", library_status, f"{sum(1 for row in library_rows if row['exists'])}/{len(library_rows)} locally available{summary_suffix}", libraries=library_rows, required_missing=required_missing)

    semantic = config.get("semantic_catalog") if isinstance(config.get("semantic_catalog"), dict) else {}
    adapters = semantic.get("read_only_adapters") if isinstance(semantic.get("read_only_adapters"), list) else []
    adapter_rows: list[dict[str, Any]] = []
    adapter_failures: list[str] = []
    adapter_pending: list[str] = []
    for adapter in adapters:
        if not isinstance(adapter, dict) or adapter.get("enabled", True) is False:
            continue
        adapter_path = Path(str(adapter.get("path") or ""))
        row: dict[str, Any] = {
            "kind": adapter.get("kind"),
            "path": str(adapter_path),
            "exists": adapter_path.is_file(),
            "allowed_use_scope": adapter.get("allowed_use_scope"),
        }
        if not adapter_path.is_file():
            (adapter_pending if remote_only_context else adapter_failures).append(
                str(adapter_path)
            )
        elif adapter.get("kind") == "synthesis_object_library":
            try:
                connection = sqlite3.connect(
                    f"file:{adapter_path}?mode=ro", uri=True
                )
                scope = str(
                    adapter.get("allowed_use_scope")
                    or "internal_noncommercial_research"
                )
                row["callable_count"] = int(connection.execute(
                    """SELECT COUNT(*) FROM objects
                       WHERE callable=1 AND qa_status='ready'
                         AND license_review_status='verified'
                         AND allowed_use_scope=?""",
                    (scope,),
                ).fetchone()[0])
                connection.close()
            except (OSError, sqlite3.Error) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                adapter_failures.append(str(adapter_path))
        adapter_rows.append(row)
    if adapter_rows:
        adapter_status = (
            "fail" if adapter_failures else ("pending" if adapter_pending else "pass")
        )
        add(
            "semantic_read_only_adapters",
            adapter_status,
            f"{len(adapter_rows) - len(adapter_failures) - len(adapter_pending)}/{len(adapter_rows)} adapters available",
            adapters=adapter_rows,
            failures=adapter_failures,
            pending=adapter_pending,
        )

    official = next((lib for lib in libraries if isinstance(lib, dict) and lib.get("name") == "isaac_official"), None)
    if official:
        official_root = Path(str(official.get("root") or ""))
        ready = official_root.exists()
        add("official_assets", "pass" if ready else "pending", str(official_root))

    statuses = [row["status"] for row in checks]
    status = "fail" if "fail" in statuses else ("pending" if "pending" in statuses else ("warn" if "warn" in statuses else "pass"))
    return {
        "status": status,
        "agent_loop_owner": "external_harness",
        "config_path": config.get("_config_path"),
        "profile": config.get("_profile"),
        "checks": checks,
    }
