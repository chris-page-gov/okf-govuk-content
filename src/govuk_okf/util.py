"""Deterministic serialization, identifiers and sharding helpers."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, TypeVar

T = TypeVar("T")


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def slugify(value: str, fallback: str = "item") -> str:
    normal = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", normal).strip("-")
    return slug[:120] or fallback


def chunks(values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(value)
    with path.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=9) as compressed:
            compressed.write(payload)


def read_gzip_json(path: Path, *, max_compressed_bytes: int = 64 * 1024 * 1024, max_uncompressed_bytes: int = 128 * 1024 * 1024) -> Any:
    if path.stat().st_size > max_compressed_bytes:
        raise ValueError(f"compressed JSON exceeds limit: {path}")
    payload = bytearray()
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(min(1024 * 1024, max_uncompressed_bytes + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > max_uncompressed_bytes:
                raise ValueError(f"decompressed JSON exceeds limit: {path}")
    return json.loads(payload.decode("utf-8"))


def fnv1a32(value: str) -> int:
    hashed = 0x811C9DC5
    for byte in value.encode("utf-8"):
        hashed ^= byte
        hashed = (hashed * 0x01000193) & 0xFFFFFFFF
    return hashed


def adjacency_bucket(route: str) -> str:
    return f"{(fnv1a32(route) >> 24) & 0xFF:02x}"


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_dump(value: Any, indent: int = 0) -> str:
    """Serialize the JSON-compatible YAML-LD subset used by this repository."""
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            rendered_key = json.dumps(str(key), ensure_ascii=False) if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else str(key)
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(f"{prefix}{rendered_key}: {'{}' if isinstance(item, dict) else '[]'}")
                else:
                    lines.append(f"{prefix}{rendered_key}:")
                    lines.append(yaml_dump(item, indent + 2))
            else:
                lines.append(f"{prefix}{rendered_key}: {yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                first_key, first_value = next(iter(item.items()))
                rendered_key = json.dumps(str(first_key), ensure_ascii=False) if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(first_key)) else str(first_key)
                if isinstance(first_value, (dict, list)):
                    lines.append(f"{prefix}- {rendered_key}:")
                    lines.append(yaml_dump(first_value, indent + 4))
                else:
                    lines.append(f"{prefix}- {rendered_key}: {yaml_scalar(first_value)}")
                for key, nested in list(item.items())[1:]:
                    rendered_nested = json.dumps(str(key), ensure_ascii=False) if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else str(key)
                    if isinstance(nested, (dict, list)):
                        if not nested:
                            lines.append(f"{' ' * (indent + 2)}{rendered_nested}: {'{}' if isinstance(nested, dict) else '[]'}")
                        else:
                            lines.append(f"{' ' * (indent + 2)}{rendered_nested}:")
                            lines.append(yaml_dump(nested, indent + 4))
                    else:
                        lines.append(f"{' ' * (indent + 2)}{rendered_nested}: {yaml_scalar(nested)}")
            elif isinstance(item, list):
                lines.append(f"{prefix}-")
                lines.append(yaml_dump(item, indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{yaml_scalar(value)}"


def _yaml_key(value: str) -> str:
    value = value.strip()
    return str(json.loads(value)) if value.startswith('"') else value


def _yaml_scalar_load(value: str) -> Any:
    value = value.strip()
    if value in {"null", "true", "false", "{}", "[]"} or value.startswith('"') or re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        return json.loads(value)
    raise ValueError(f"unsupported YAML-LD scalar in deterministic subset: {value}")


def yaml_load_subset(text: str) -> Any:
    """Parse exactly the safe JSON-compatible YAML subset emitted by yaml_dump."""
    raw_lines = [(len(line) - len(line.lstrip(" ")), line.lstrip(" ")) for line in text.splitlines() if line.strip()]
    if not raw_lines:
        raise ValueError("empty YAML-LD document")

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(raw_lines) or raw_lines[index][0] != indent:
            raise ValueError(f"invalid YAML-LD indentation at line {index + 1}")
        is_list = raw_lines[index][1].startswith("-")
        container: Any = [] if is_list else {}
        while index < len(raw_lines):
            current_indent, content = raw_lines[index]
            if current_indent < indent:
                break
            if current_indent != indent:
                raise ValueError(f"unexpected YAML-LD indentation at line {index + 1}")
            if is_list:
                if not content.startswith("-"):
                    raise ValueError(f"mixed sequence and mapping at line {index + 1}")
                remainder = content[1:].strip()
                if not remainder:
                    value, index = parse_block(index + 1, indent + 2)
                    container.append(value)
                    continue
                container.append(_yaml_scalar_load(remainder))
                index += 1
                continue
            if content.startswith("-") or ":" not in content:
                raise ValueError(f"invalid YAML-LD mapping at line {index + 1}")
            key_text, remainder = content.split(":", 1)
            key = _yaml_key(key_text)
            if key in container:
                raise ValueError(f"duplicate YAML-LD key: {key}")
            remainder = remainder.strip()
            if remainder:
                container[key] = _yaml_scalar_load(remainder)
                index += 1
            else:
                if index + 1 >= len(raw_lines) or raw_lines[index + 1][0] <= indent:
                    raise ValueError(f"missing YAML-LD value for {key}")
                value, index = parse_block(index + 1, raw_lines[index + 1][0])
                container[key] = value
        return container, index

    result, end = parse_block(0, raw_lines[0][0])
    if end != len(raw_lines):
        raise ValueError("trailing YAML-LD content")
    return result
