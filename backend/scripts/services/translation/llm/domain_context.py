from __future__ import annotations
import json
import os
import re
from pathlib import Path

import fitz

from foundation.shared.prompt_loader import load_prompt

from .deepseek_client import request_chat_content
from .structured_models import DOMAIN_CONTEXT_RESPONSE_SCHEMA
from .structured_parsers import parse_domain_context_response


DOMAIN_CONTEXT_FILE_NAME = "domain-context.json"
DOMAIN_CONTEXT_RAW_FILE_NAME = "domain-context.raw.txt"
DEFAULT_DOMAIN_CONTEXT_PAGES = 5
DEFAULT_DOMAIN_CONTEXT_MAX_CHARS = 16000
DEFAULT_DOMAIN_CONTEXT_HEADING_MAX_CHARS = 6000
HEADING_ROLE_NAMES = {
    "abstract",
    "heading",
    "section_heading",
    "subsection_heading",
    "title",
}
TOC_MARKER_RE = re.compile(r"\b(?:contents|table\s+of\s+contents)\b|目录", re.IGNORECASE)
TOC_LINE_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z])\s+.{2,}?(?:\.{2,}|\s{2,})\s*\d+\s*$")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _truncate_text(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if limit <= 0 or len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "\n[truncated]"


def _compact_lines(lines: list[str], *, max_chars: int) -> str:
    parts: list[str] = []
    total = 0
    for raw in lines:
        line = " ".join(str(raw or "").split()).strip()
        if not line:
            continue
        next_len = len(line) + 1
        if max_chars > 0 and parts and total + next_len > max_chars:
            break
        parts.append(line)
        total += next_len
    return "\n".join(parts).strip()


def extract_pdf_preview_text(
    source_pdf_path: Path,
    max_pages: int = DEFAULT_DOMAIN_CONTEXT_PAGES,
    *,
    max_chars: int = DEFAULT_DOMAIN_CONTEXT_MAX_CHARS,
) -> str:
    doc = fitz.open(source_pdf_path)
    try:
        parts: list[str] = []
        for page_idx in range(min(max_pages, len(doc))):
            page = doc[page_idx]
            text = page.get_text("text").strip()
            if text:
                parts.append(f"[Page {page_idx + 1}]\n{text}")
        return _truncate_text("\n\n".join(parts).strip(), max_chars)
    finally:
        doc.close()


def _metadata_role(item: dict) -> str:
    metadata = item.get("metadata", {}) or {}
    for key in ("structure_role", "semantic_role", "role"):
        value = str(metadata.get(key, "") or "").strip().lower()
        if value:
            return value
    return ""


def _item_text(item: dict) -> str:
    return str(item.get("text") or item.get("source_text") or item.get("protected_source_text") or "").strip()


def _looks_like_heading(item: dict) -> bool:
    role = _metadata_role(item)
    if role in HEADING_ROLE_NAMES:
        return True
    block_type = str(item.get("block_type", "") or "").strip().lower()
    text = " ".join(_item_text(item).split())
    if block_type in {"title", "heading"} and text:
        return True
    if not text or len(text) > 180:
        return False
    if re.match(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.?)\s+[A-Z][^\n]{3,}$", text):
        return True
    words = re.findall(r"[A-Za-z]+", text)
    return bool(words) and len(words) <= 12 and sum(word[:1].isupper() for word in words) >= max(2, len(words) // 2)


def _looks_like_toc_line(text: str) -> bool:
    line = " ".join(str(text or "").split()).strip()
    if not line or len(line) > 220:
        return False
    return bool(TOC_LINE_RE.match(line))


def extract_document_outline_preview(
    data: dict,
    *,
    max_chars: int | None = None,
) -> str:
    if max_chars is None:
        max_chars = _env_int(
            "PDF_TRANSLATOR_DOMAIN_CONTEXT_HEADING_MAX_CHARS",
            DEFAULT_DOMAIN_CONTEXT_HEADING_MAX_CHARS,
            minimum=1000,
        )
    pages = data.get("pages", []) if isinstance(data, dict) else []
    if not isinstance(pages, list):
        return ""
    heading_lines: list[str] = []
    toc_lines: list[str] = []
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        raw_items = page.get("items") or page.get("blocks") or page.get("text_items") or []
        if not isinstance(raw_items, list):
            continue
        page_texts: list[str] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = _item_text(item)
            if not text:
                continue
            page_texts.append(text)
            if _looks_like_heading(item):
                heading_lines.append(f"[Page {page_index + 1}] {text}")
            if _looks_like_toc_line(text):
                toc_lines.append(f"[Page {page_index + 1}] {text}")
        page_joined = "\n".join(page_texts)
        if TOC_MARKER_RE.search(page_joined):
            for text in page_texts:
                if len(text) <= 220:
                    toc_lines.append(f"[Page {page_index + 1}] {text}")
    sections = []
    toc_text = _compact_lines(toc_lines, max_chars=max_chars // 2)
    heading_text = _compact_lines(heading_lines, max_chars=max_chars)
    if toc_text:
        sections.append("[Detected table of contents candidates]\n" + toc_text)
    if heading_text:
        sections.append("[Detected title/heading candidates]\n" + heading_text)
    return "\n\n".join(sections).strip()


def build_domain_inference_messages(preview_text: str, outline_text: str = "") -> list[dict[str, str]]:
    user_payload = {
        "task": load_prompt("domain_inference_task.txt"),
        "preview_text": preview_text,
    }
    if outline_text.strip():
        user_payload["outline_text"] = outline_text.strip()
    return [
        {"role": "system", "content": load_prompt("domain_inference_system.txt")},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def load_cached_domain_context(output_dir: Path | None) -> dict[str, str] | None:
    if output_dir is None:
        return None
    path = output_dir / DOMAIN_CONTEXT_FILE_NAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "domain": str(payload.get("domain", "")).strip(),
        "summary": str(payload.get("summary", "")).strip(),
        "translation_guidance": str(payload.get("translation_guidance", "")).strip(),
        "preview_text": str(payload.get("preview_text", "") or ""),
        "outline_text": str(payload.get("outline_text", "") or ""),
    }


def infer_domain_context_from_preview_text(
    *,
    preview_text: str,
    outline_text: str = "",
    api_key: str,
    model: str,
    base_url: str,
    output_dir: Path | None = None,
) -> dict[str, str]:
    if not preview_text and not outline_text:
        result = {
            "domain": "",
            "summary": "",
            "translation_guidance": "",
            "preview_text": "",
            "outline_text": "",
        }
        if output_dir is not None:
            save_domain_context(output_dir, result)
        return result
    cached = load_cached_domain_context(output_dir)
    if (
        cached is not None
        and str(cached.get("preview_text", "") or "").strip() == preview_text.strip()
        and str(cached.get("outline_text", "") or "").strip() == outline_text.strip()
    ):
        print("domain-infer: cache hit", flush=True)
        return cached

    content = request_chat_content(
        build_domain_inference_messages(preview_text, outline_text=outline_text),
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0.0,
        response_format=DOMAIN_CONTEXT_RESPONSE_SCHEMA,
        timeout=120,
        request_label="domain-infer",
    )
    try:
        result = parse_domain_context_response(content, preview_text=preview_text)
        result["outline_text"] = outline_text
    except Exception:
        if output_dir is not None:
            save_domain_context_raw(output_dir, content)
        raise
    if output_dir is not None:
        save_domain_context(output_dir, result)
    return result


def infer_domain_context(
    *,
    source_pdf_path: Path | None,
    api_key: str,
    model: str,
    base_url: str,
    preview_text_fallback: str = "",
    outline_text: str = "",
    output_dir: Path | None = None,
    preview_pages: int | None = None,
    preview_max_chars: int | None = None,
) -> dict[str, str]:
    preview_pages = (
        _env_int("PDF_TRANSLATOR_DOMAIN_CONTEXT_PAGES", DEFAULT_DOMAIN_CONTEXT_PAGES, minimum=1)
        if preview_pages is None
        else max(1, int(preview_pages))
    )
    preview_max_chars = (
        _env_int(
            "PDF_TRANSLATOR_DOMAIN_CONTEXT_MAX_CHARS",
            DEFAULT_DOMAIN_CONTEXT_MAX_CHARS,
            minimum=1000,
        )
        if preview_max_chars is None
        else max(1000, int(preview_max_chars))
    )
    preview_text = (
        extract_pdf_preview_text(source_pdf_path, max_pages=preview_pages, max_chars=preview_max_chars)
        if source_pdf_path is not None
        else ""
    )
    if not preview_text:
        preview_text = _truncate_text(preview_text_fallback.strip(), preview_max_chars)
    return infer_domain_context_from_preview_text(
        preview_text=preview_text,
        outline_text=outline_text,
        api_key=api_key,
        model=model,
        base_url=base_url,
        output_dir=output_dir,
    )


def save_domain_context(output_dir: Path, context: dict[str, str]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / DOMAIN_CONTEXT_FILE_NAME
    path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_domain_context_raw(output_dir: Path, content: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / DOMAIN_CONTEXT_RAW_FILE_NAME
    path.write_text(content or "", encoding="utf-8")
    return path
