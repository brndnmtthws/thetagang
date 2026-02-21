from enum import Enum
from typing import Any, Mapping

from tomlkit import parse
from tomlkit.exceptions import ParseError


class SchemaKind(str, Enum):
    v1 = "v1"
    v2 = "v2"
    unknown = "unknown"


def detect_schema(raw_text: str) -> SchemaKind:
    try:
        doc = parse(raw_text)
    except ParseError:
        return SchemaKind.unknown

    meta = _get_mapping(doc, "meta")
    if meta and meta.get("schema_version") == 2:
        return SchemaKind.v2

    # Heuristic for legacy schema.
    legacy_keys = {"account", "symbols", "target", "roll_when"}
    if legacy_keys.issubset(set(doc.keys())):
        return SchemaKind.v1

    return SchemaKind.unknown


def _get_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    item = mapping.get(key)
    if isinstance(item, Mapping):
        return item
    return None
