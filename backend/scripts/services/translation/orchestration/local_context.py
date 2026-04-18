from __future__ import annotations

import os
import re

from services.translation.continuation.rules import normalize_text


DEFAULT_LOCAL_CONTEXT_NEIGHBORS = 1
DEFAULT_LOCAL_CONTEXT_CHARS = 420
CONTEXT_BLOCK_TYPES = {"text", "title", "heading", "image_caption", "table_caption"}
PLACEHOLDER_RE = re.compile(r"<[ft]\d+-[0-9a-z]{3}/>|\[\[FORMULA_\d+]]|@@P\d+@@")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _context_text(item: dict) -> str:
    text = str(item.get("protected_source_text") or item.get("source_text") or "")
    text = PLACEHOLDER_RE.sub(" ", text)
    return normalize_text(text)


def _is_context_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if str(item.get("block_type", "") or "").strip().lower() not in CONTEXT_BLOCK_TYPES:
        return False
    if not bool(item.get("should_translate", True)):
        return False
    metadata = item.get("metadata", {}) or {}
    role = str(metadata.get("structure_role", "") or metadata.get("semantic_role", "") or "").strip().lower()
    if role in {"reference_entry", "metadata", "footer", "header", "page_number"}:
        return False
    return bool(_context_text(item))


def _truncate_context(text: str, max_chars: int) -> str:
    compact = normalize_text(text)
    if max_chars <= 0 or len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def _collect_neighbors(
    items: list[dict],
    index: int,
    *,
    direction: int,
    count: int,
    max_chars: int,
) -> str:
    parts: list[str] = []
    cursor = index + direction
    while 0 <= cursor < len(items) and len(parts) < count:
        candidate = items[cursor]
        if _is_context_item(candidate):
            parts.append(_context_text(candidate))
        cursor += direction
    if direction < 0:
        parts.reverse()
    return _truncate_context(" ".join(part for part in parts if part), max_chars)


def annotate_local_context(
    page_payloads: dict[int, list[dict]],
    *,
    neighbor_count: int | None = None,
    max_chars: int | None = None,
) -> int:
    count = (
        _env_int("PDF_TRANSLATOR_LOCAL_CONTEXT_NEIGHBORS", DEFAULT_LOCAL_CONTEXT_NEIGHBORS, minimum=0)
        if neighbor_count is None
        else max(0, neighbor_count)
    )
    chars = (
        _env_int("PDF_TRANSLATOR_LOCAL_CONTEXT_CHARS", DEFAULT_LOCAL_CONTEXT_CHARS, minimum=0)
        if max_chars is None
        else max(0, max_chars)
    )
    flat_items = [item for page_idx in sorted(page_payloads) for item in page_payloads[page_idx]]
    annotated = 0
    for index, item in enumerate(flat_items):
        before = _collect_neighbors(flat_items, index, direction=-1, count=count, max_chars=chars)
        after = _collect_neighbors(flat_items, index, direction=1, count=count, max_chars=chars)
        if item.get("local_context_before") != before:
            item["local_context_before"] = before
            annotated += 1
        if item.get("local_context_after") != after:
            item["local_context_after"] = after
            annotated += 1
    return annotated


__all__ = ["annotate_local_context"]
