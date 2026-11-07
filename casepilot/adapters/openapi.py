"""OpenAPI 3.x 文档解析：paths / parameters / requestBody / schema → 文本要点。

解析策略（确定性规则）：
- 功能点：paths 下每个 method+path 操作（kind=api），取 summary / operationId 作描述
- 数字边界：parameters / requestBody / components.schemas 中的
  minimum / maximum / minLength / maxLength / minItems / maxItems 等约束
- 主流程：按文档声明顺序取前几个接口合成调用序列（场景法基本流素材）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

import yaml

from ..core.schema import Boundary, DocumentProfile, Feature

# OpenAPI 标准 HTTP 方法（按文档遍历顺序识别）
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# schema 数字 / 长度约束 → 边界类型映射
_CONSTRAINT_MAP = {
    "minimum": ("range_min", ""),
    "maximum": ("range_max", ""),
    "minLength": ("range_min", "字符"),
    "maxLength": ("range_max", "字符"),
    "minItems": ("range_min", "项"),
    "maxItems": ("range_max", "项"),
    "minProperties": ("range_min", "个属性"),
    "maxProperties": ("range_max", "个属性"),
}

_MAX_DEPTH = 8  # 递归扫描 schema 的深度上限，防止自引用死循环


def _load_document(path: Path) -> dict:
    """按扩展名加载 YAML / JSON 文档。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI 文档结构非法：顶层应为对象（mapping），实际为 {type(data).__name__}")
    return data


def _scan_schema_constraints(
    schema: Any, context: str, boundaries: list[Boundary], depth: int, seen: set[int]
) -> None:
    """递归扫描 schema 中的数值 / 长度约束，生成边界列表。"""
    if depth > _MAX_DEPTH or not isinstance(schema, dict) or id(schema) in seen:
        return
    seen.add(id(schema))

    for key, (kind, unit) in _CONSTRAINT_MAP.items():
        value = schema.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            boundaries.append(
                Boundary(text=f"{context} 的 {key} 约束", value=float(value), kind=kind, unit=unit)
            )

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop_name, prop_schema in properties.items():
            _scan_schema_constraints(prop_schema, f"{context}.{prop_name}", boundaries, depth + 1, seen)
    for key in ("items", "additionalProperties"):
        if key in schema:
            _scan_schema_constraints(schema[key], f"{context}.{key}", boundaries, depth + 1, seen)
    for key in ("allOf", "anyOf", "oneOf"):
        sub = schema.get(key)
        if isinstance(sub, list):
            for i, item in enumerate(sub):
                _scan_schema_constraints(item, f"{context}.{key}[{i}]", boundaries, depth + 1, seen)


def _extract_boundaries(data: dict) -> list[Boundary]:
    """从 parameters / requestBody / responses / components 提取数字边界。"""
    boundaries: list[Boundary] = []
    seen: set[int] = set()

    paths = data.get("paths")
    if isinstance(paths, dict):
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            params = path_item.get("parameters")
            if isinstance(params, list):
                for param in params:
                    if isinstance(param, dict) and isinstance(param.get("schema"), dict):
                        name = param.get("name", "参数")
                        _scan_schema_constraints(
                            param["schema"], f"{path} 参数 {name}", boundaries, 0, seen
                        )
            for method in _HTTP_METHODS:
                op = path_item.get(method)
                if not isinstance(op, dict):
                    continue
                for field, label in (("requestBody", "请求体"), ("responses", "响应")):
                    section = op.get(field)
                    if isinstance(section, dict):
                        _scan_schema_constraints(section, f"{path} {label}", boundaries, 0, seen)

    components = data.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            for name, schema in schemas.items():
                _scan_schema_constraints(schema, f"schema {name}", boundaries, 0, seen)

    # 去重：同值 / 同类型 / 同单位视为同一边界（保持出现顺序）
    seen_b: set[tuple[float, str, str]] = set()
    deduped: list[Boundary] = []
    for b in boundaries:
        key = (b.value, b.kind, b.unit)
        if key not in seen_b:
            seen_b.add(key)
            deduped.append(b)
    return deduped


def _extract_features(data: dict) -> list[Feature]:
    """从 paths 提取接口功能点（kind=api）。"""
    features: list[Feature] = []
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return features
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            summary = (op.get("summary") or op.get("operationId") or "").strip()
            description = (op.get("description") or summary).strip()
            name = f"{method.upper()} {path}"
            if summary:
                name = f"{name}（{summary}）"
            features.append(Feature(name=name, description=description, kind="api"))
    return features


def _extract_flows(data: dict, features: list[Feature]) -> list[str]:
    """按接口声明顺序合成主流程调用序列（供场景法使用）。"""
    flows = [f"调用 {f.name}" for f in features[:3]]
    if not flows:
        title = _doc_title(data)
        flows = [f"完成「{title}」的核心接口调用"]
    return flows


def _doc_title(data: dict) -> str:
    """取 info.title，缺失时返回默认值。"""
    info = data.get("info")
    if isinstance(info, dict):
        title = str(info.get("title") or "").strip()
        if title:
            return title
    return "OpenAPI 接口"


def parse_dict(data: dict, source: str = "OpenAPI 文档") -> DocumentProfile:
    """把已加载的 OpenAPI 字典解析为 DocumentProfile。"""
    title = _doc_title(data)
    features = _extract_features(data)
    boundaries = _extract_boundaries(data)
    flows = _extract_flows(data, features)

    return DocumentProfile(
        source=source,
        source_type="openapi",
        module=title,
        features=features,
        boundaries=boundaries,
        flows=flows,
    )


def parse_file(path: Union[str, Path]) -> DocumentProfile:
    """解析 OpenAPI 文件（.yaml / .yml / .json）。"""
    p = Path(path)
    data = _load_document(p)
    return parse_dict(data, source=str(p))
