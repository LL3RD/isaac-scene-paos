from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "room_shells.json"
AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


def load_library(path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path else DEFAULT_LIBRARY
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != "isaac-room-shell-library/v1":
        raise ValueError(f"Unsupported room-shell library: {source}")
    if not isinstance(data.get("presets"), dict):
        raise ValueError(f"Room-shell library has no presets: {source}")
    return data


def list_presets(path: str | Path | None = None) -> list[dict[str, Any]]:
    library = load_library(path)
    return [
        {
            "id": preset_id,
            "domain": row.get("domain"),
            "room_types": deepcopy(row.get("room_types") or []),
            "description": row.get("description"),
            "architectural_element_template_count": len(row.get("elements") or []),
            "fixture_count": len((row.get("lighting") or {}).get("fixtures") or []),
        }
        for preset_id, row in sorted(library["presets"].items())
    ]


def _normalized_point(values: list[float], room: dict[str, Any]) -> list[float]:
    width, depth, height = [float(value) for value in room["size_m"]]
    center = room.get("center_m") or [0.0, 0.0, 0.0]
    return [
        float(center[0]) + float(values[0]) * width,
        float(center[1]) + float(values[1]) * depth,
        float(center[2]) + float(values[2]) * height,
    ]


def _normalized_size(
    ratios: list[float],
    minimum: list[float] | None,
    room: dict[str, Any],
) -> list[float]:
    dimensions = [float(value) for value in room["size_m"]]
    minimum = minimum or [0.0, 0.0, 0.0]
    return [
        max(float(ratios[index]) * dimensions[index], float(minimum[index]))
        for index in range(3)
    ]


def _expanded_positions(template: dict[str, Any]) -> list[tuple[str, list[float]]]:
    base = [float(value) for value in template["translate_norm"]]
    repeat = template.get("repeat")
    if isinstance(repeat, dict):
        count = max(1, int(repeat.get("count") or 1))
        axis = AXIS_INDEX[str(repeat.get("axis") or "X").upper()]
        start = float(repeat.get("start_norm", base[axis]))
        end = float(repeat.get("end_norm", base[axis]))
        values = [start] if count == 1 else [
            start + (end - start) * index / (count - 1)
            for index in range(count)
        ]
        result = []
        for index, value in enumerate(values, start=1):
            position = list(base)
            position[axis] = value
            result.append((f"_{index:02d}", position))
        return result
    grid = template.get("repeat_grid")
    if isinstance(grid, dict):
        xs = grid.get("x_norm") or [base[0]]
        ys = grid.get("y_norm") or [base[1]]
        zs = grid.get("z_norm") or [base[2]]
        result = []
        index = 0
        for x_value in xs:
            for y_value in ys:
                for z_value in zs:
                    index += 1
                    result.append((f"_{index:02d}", [float(x_value), float(y_value), float(z_value)]))
        return result
    return [("", base)]


def _expand_elements(preset: dict[str, Any], room: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for template in preset.get("elements") or []:
        if not isinstance(template, dict):
            continue
        for suffix, position in _expanded_positions(template):
            row = {
                key: deepcopy(value)
                for key, value in template.items()
                if key not in {"translate_norm", "size_norm", "min_size_m", "repeat", "repeat_grid"}
            }
            row["id"] = f"{template['id']}{suffix}"
            row["translate"] = _normalized_point(position, room)
            row["size"] = _normalized_size(
                template["size_norm"],
                template.get("min_size_m"),
                room,
            )
            row.setdefault("physics", "static")
            row.setdefault("evidence", f"room-shell preset {preset.get('id', 'unknown')}")
            rows.append(row)
    return rows


def _expand_lighting(preset: dict[str, Any], room: dict[str, Any]) -> dict[str, Any] | None:
    raw = preset.get("lighting")
    if not isinstance(raw, dict):
        return None
    lighting = deepcopy(raw)
    fixtures = []
    width, depth, _height = [float(value) for value in room["size_m"]]
    for fixture in raw.get("fixtures") or []:
        row = {
            key: deepcopy(value)
            for key, value in fixture.items()
            if key not in {"translate_norm", "look_at_norm", "width_ratio", "height_ratio"}
        }
        if fixture.get("translate_norm") is not None:
            row["translate"] = _normalized_point(fixture["translate_norm"], room)
        if fixture.get("look_at_norm") is not None:
            row["look_at"] = _normalized_point(fixture["look_at_norm"], room)
        if fixture.get("width_ratio") is not None:
            row["width"] = float(fixture["width_ratio"]) * width
        if fixture.get("height_ratio") is not None:
            row["height"] = float(fixture["height_ratio"]) * depth
        fixtures.append(row)
    lighting["fixtures"] = fixtures
    lighting["evidence"] = f"room-shell preset {preset.get('id', 'unknown')}"
    return lighting


def apply_preset(
    brief: dict[str, Any],
    preset_id: str,
    *,
    library_path: str | Path | None = None,
) -> dict[str, Any]:
    result = deepcopy(brief)
    room = result.get("room")
    if not isinstance(room, dict) or not room.get("size_m"):
        raise ValueError("Room-shell presets require room.size_m")
    library = load_library(library_path)
    source = library["presets"].get(preset_id)
    if not isinstance(source, dict):
        raise ValueError(f"Unknown room-shell preset: {preset_id}")
    preset = deepcopy(source)
    preset["id"] = preset_id

    surfaces = preset.get("surfaces") or {}
    for key in ("floor", "walls", "ceiling"):
        if key not in surfaces:
            continue
        explicit = room.get(key) if isinstance(room.get(key), dict) else {}
        merged = deepcopy(surfaces[key])
        merged.update(deepcopy(explicit))
        room[key] = merged

    options = room.get("shell_preset_options") if isinstance(room.get("shell_preset_options"), dict) else {}
    if not options.get("preserve_open_sides", False):
        room["open_sides"] = deepcopy(preset.get("open_sides") or ["front"])

    preset_elements = _expand_elements(preset, room)
    explicit_elements = [
        deepcopy(row)
        for row in room.get("architectural_elements") or []
        if isinstance(row, dict)
    ]
    if options.get("preserve_architectural_elements", False):
        by_id = {str(row.get("id")): row for row in preset_elements}
        by_id.update({str(row.get("id")): row for row in explicit_elements})
        room["architectural_elements"] = list(by_id.values())
    else:
        room["architectural_elements"] = preset_elements

    room["shell_preset"] = preset_id
    room["shell_preset_resolved"] = {
        "id": preset_id,
        "library_schema_version": library["schema_version"],
        "description": preset.get("description"),
        "object_free": True,
    }
    if not isinstance(result.get("lighting"), dict) or options.get("replace_lighting", False):
        lighting = _expand_lighting(preset, room)
        if lighting is not None:
            result["lighting"] = lighting
    return result


def resolve_declared_preset(
    brief: dict[str, Any],
    *,
    library_path: str | Path | None = None,
) -> dict[str, Any]:
    room = brief.get("room") if isinstance(brief.get("room"), dict) else {}
    preset_id = room.get("shell_preset")
    if not preset_id:
        return deepcopy(brief)
    return apply_preset(brief, str(preset_id), library_path=library_path)
