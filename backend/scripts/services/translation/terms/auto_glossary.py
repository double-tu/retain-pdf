from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
from typing import Any

from foundation.shared.prompt_loader import load_prompt
from services.mineru.artifacts import save_json
from services.translation.llm.deepseek_client import request_chat_content
from services.translation.terms.glossary import GlossaryEntry
from services.translation.terms.glossary import normalize_glossary_entries


AUTO_GLOSSARY_FILE_NAME = "auto-glossary.json"
AUTO_GLOSSARY_RAW_FILE_NAME = "auto-glossary.raw.txt"
DEFAULT_AUTO_GLOSSARY_CANDIDATES = 100
DEFAULT_AUTO_GLOSSARY_TERMS = 30
DEFAULT_AUTO_GLOSSARY_CONTEXTS = 2
DEFAULT_AUTO_GLOSSARY_CONTEXT_CHARS = 160
MIN_TERM_FREQUENCY = 2
AUTO_GLOSSARY_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "auto_glossary_response",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "level": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["source", "target", "level", "note"],
                    },
                }
            },
            "required": ["entries"],
        },
    },
}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")
ACRONYM_RE = re.compile(r"^[A-Z][A-Z0-9-]{1,}$")
GENERIC_WORDS = {
    "ability",
    "according",
    "acid",
    "analysis",
    "approach",
    "article",
    "based",
    "case",
    "change",
    "chapter",
    "common",
    "comparison",
    "condition",
    "data",
    "different",
    "discussion",
    "effect",
    "example",
    "experiment",
    "figure",
    "following",
    "general",
    "important",
    "increase",
    "information",
    "large",
    "method",
    "model",
    "number",
    "paper",
    "parameter",
    "part",
    "process",
    "property",
    "reaction",
    "reference",
    "result",
    "section",
    "shown",
    "small",
    "study",
    "system",
    "table",
    "term",
    "type",
    "value",
    "work",
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "between",
    "by",
    "can",
    "could",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "more",
    "not",
    "of",
    "on",
    "or",
    "our",
    "than",
    "that",
    "the",
    "their",
    "these",
    "this",
    "to",
    "using",
    "was",
    "were",
    "which",
    "with",
    "within",
}
BAD_SUFFIXES = {
    "able",
    "al",
    "ed",
    "ful",
    "ic",
    "ical",
    "ing",
    "ive",
    "less",
    "ly",
    "ous",
}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def auto_glossary_enabled() -> bool:
    return os.environ.get("PDF_TRANSLATOR_AUTO_GLOSSARY", "1").strip().lower() not in {"0", "false", "no", "off"}


def _item_text(item: dict) -> str:
    return str(
        item.get("source_text")
        or item.get("protected_source_text")
        or item.get("translation_unit_protected_source_text")
        or ""
    )


def _is_reference_or_noise(item: dict) -> bool:
    label = str(item.get("classification_label", "") or "").strip().lower()
    if "reference" in label or "metadata" in label:
        return True
    metadata = item.get("metadata", {}) or {}
    role = str(metadata.get("structure_role", "") or metadata.get("semantic_role", "") or "").strip().lower()
    return role in {"reference_entry", "metadata", "footer", "header", "page_number"}


def _iter_source_items(page_payloads: dict[int, list[dict]]) -> list[dict]:
    items: list[dict] = []
    for page_idx in sorted(page_payloads):
        for item in page_payloads[page_idx]:
            if not isinstance(item, dict):
                continue
            if _is_reference_or_noise(item):
                continue
            if str(item.get("block_type", "") or "").strip().lower() not in {"text", "title", "heading", "image_caption", "table_caption"}:
                continue
            text = _item_text(item)
            if len(text) < 12:
                continue
            items.append(item)
    return items


def _tokenize(text: str) -> list[str]:
    return [match.group(0) for match in WORD_RE.finditer(text or "")]


def _valid_edge_token(token: str) -> bool:
    normalized = token.casefold()
    return bool(normalized) and normalized not in STOPWORDS


def _looks_like_bad_single_word(token: str) -> bool:
    normalized = token.casefold()
    if len(normalized) < 4:
        return True
    if normalized in STOPWORDS or normalized in GENERIC_WORDS:
        return True
    if ACRONYM_RE.match(token):
        return False
    if any(normalized.endswith(suffix) for suffix in BAD_SUFFIXES):
        return True
    return False


def _normalize_term(tokens: list[str]) -> str:
    return " ".join(tokens).strip()


def _candidate_terms_from_text(text: str) -> set[str]:
    tokens = _tokenize(text)
    terms: set[str] = set()
    for ngram_size in (1, 2, 3, 4):
        if len(tokens) < ngram_size:
            continue
        for index in range(0, len(tokens) - ngram_size + 1):
            window = tokens[index : index + ngram_size]
            if not _valid_edge_token(window[0]) or not _valid_edge_token(window[-1]):
                continue
            term = _normalize_term(window)
            if not term:
                continue
            if ngram_size == 1 and _looks_like_bad_single_word(term):
                continue
            if ngram_size > 1:
                lowered = [token.casefold() for token in window]
                if all(token in STOPWORDS or token in GENERIC_WORDS for token in lowered):
                    continue
                if sum(token not in STOPWORDS for token in lowered) < 2:
                    continue
            terms.add(term)
    return terms


def _term_key(term: str) -> str:
    return " ".join(term.casefold().split())


def _short_context(text: str, term: str, *, limit: int) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if len(compact) <= limit:
        return compact
    lower = compact.casefold()
    pos = lower.find(term.casefold())
    if pos < 0:
        return compact[:limit].rstrip()
    start = max(0, pos - limit // 3)
    end = min(len(compact), start + limit)
    return compact[start:end].strip()


def _score_term(term: str, frequency: int, contexts: list[dict]) -> float:
    words = term.split()
    score = float(frequency)
    if len(words) > 1:
        score += 3.0 + min(4, len(words))
    if ACRONYM_RE.match(term.replace(" ", "")):
        score += 4.0
    if "-" in term:
        score += 1.5
    if any(ctx.get("heading") for ctx in contexts):
        score += 4.0
    if any(int(ctx.get("page_idx", 99) or 99) < 5 for ctx in contexts):
        score += 1.0
    if len(words) == 1 and term.casefold() in GENERIC_WORDS:
        score -= 5.0
    return score


def extract_auto_glossary_candidates(
    page_payloads: dict[int, list[dict]],
    *,
    max_candidates: int = DEFAULT_AUTO_GLOSSARY_CANDIDATES,
    max_contexts: int = DEFAULT_AUTO_GLOSSARY_CONTEXTS,
    context_chars: int = DEFAULT_AUTO_GLOSSARY_CONTEXT_CHARS,
) -> list[dict[str, object]]:
    counter: Counter[str] = Counter()
    display_by_key: dict[str, str] = {}
    contexts_by_key: dict[str, list[dict[str, object]]] = {}
    for item in _iter_source_items(page_payloads):
        text = _item_text(item)
        terms = _candidate_terms_from_text(text)
        heading = str(item.get("block_type", "") or "").strip().lower() in {"title", "heading"}
        metadata = item.get("metadata", {}) or {}
        role = str(metadata.get("structure_role", "") or metadata.get("semantic_role", "") or "").strip().lower()
        if role in {"title", "heading", "section_heading", "subsection_heading"}:
            heading = True
        for term in terms:
            key = _term_key(term)
            counter[key] += 1
            display_by_key.setdefault(key, term)
            contexts = contexts_by_key.setdefault(key, [])
            if len(contexts) < max_contexts:
                contexts.append(
                    {
                        "page_idx": int(item.get("page_idx", -1) or -1),
                        "heading": heading,
                        "text": _short_context(text, term, limit=context_chars),
                    }
                )
    candidates = []
    for key, frequency in counter.items():
        if frequency < MIN_TERM_FREQUENCY and not ACRONYM_RE.match(display_by_key.get(key, "")):
            continue
        term = display_by_key[key]
        contexts = contexts_by_key.get(key, [])
        candidates.append(
            {
                "term": term,
                "frequency": frequency,
                "score": round(_score_term(term, frequency, contexts), 4),
                "contexts": contexts,
            }
        )
    candidates.sort(key=lambda item: (-float(item["score"]), -int(item["frequency"]), str(item["term"]).casefold()))
    return candidates[:max_candidates]


def _build_auto_glossary_messages(
    *,
    candidates: list[dict[str, object]],
    domain_guidance: str,
    document_summary: str,
    max_terms: int,
) -> list[dict[str, str]]:
    payload = {
        "task": load_prompt("auto_glossary_task.txt"),
        "max_terms": max_terms,
        "domain_guidance": domain_guidance,
        "document_summary": document_summary,
        "candidates": candidates,
    }
    return [
        {"role": "system", "content": load_prompt("auto_glossary_system.txt")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _parse_auto_glossary_response(content: str, allowed_terms: set[str]) -> list[GlossaryEntry]:
    payload = json.loads(content)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        entries = payload.get("glossary") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    allowed = {term.casefold(): term for term in allowed_terms}
    normalized_payload = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source", "") or "").strip()
        if not source or source.casefold() not in allowed:
            continue
        target = str(entry.get("target", "") or "").strip()
        if not target:
            continue
        normalized_payload.append(
            {
                "source": allowed[source.casefold()],
                "target": target,
                "level": str(entry.get("level", "preferred") or "preferred"),
                "match_mode": "case_insensitive",
                "note": str(entry.get("note", "") or "").strip(),
            }
        )
    return normalize_glossary_entries(normalized_payload)


def generate_auto_glossary(
    *,
    page_payloads: dict[int, list[dict]],
    output_dir: Path,
    api_key: str,
    model: str,
    base_url: str,
    domain_guidance: str = "",
    document_summary: str = "",
    existing_entries: list[GlossaryEntry] | None = None,
    enabled: bool | None = None,
    max_candidates: int | None = None,
    max_terms: int | None = None,
) -> list[GlossaryEntry]:
    if enabled is None:
        enabled = auto_glossary_enabled()
    if not enabled:
        return []
    max_candidates = (
        _env_int("PDF_TRANSLATOR_AUTO_GLOSSARY_CANDIDATES", DEFAULT_AUTO_GLOSSARY_CANDIDATES, minimum=10)
        if max_candidates is None
        else max(10, int(max_candidates))
    )
    max_terms = (
        _env_int("PDF_TRANSLATOR_AUTO_GLOSSARY_TERMS", DEFAULT_AUTO_GLOSSARY_TERMS, minimum=0)
        if max_terms is None
        else max(0, int(max_terms))
    )
    if max_terms <= 0:
        return []
    candidates = extract_auto_glossary_candidates(page_payloads, max_candidates=max_candidates)
    existing_sources = {entry.source.casefold() for entry in existing_entries or []}
    candidates = [item for item in candidates if str(item.get("term", "") or "").casefold() not in existing_sources]
    manifest_path = output_dir / AUTO_GLOSSARY_FILE_NAME
    raw_path = output_dir / AUTO_GLOSSARY_RAW_FILE_NAME
    if not candidates:
        save_json(manifest_path, {"enabled": True, "candidates": [], "entries": []})
        return []
    try:
        content = request_chat_content(
            _build_auto_glossary_messages(
                candidates=candidates,
                domain_guidance=domain_guidance,
                document_summary=document_summary,
                max_terms=max_terms,
            ),
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=0.0,
            response_format=AUTO_GLOSSARY_RESPONSE_SCHEMA,
            timeout=120,
            request_label="auto-glossary",
        )
        entries = _parse_auto_glossary_response(content, {str(item["term"]) for item in candidates})
    except Exception as exc:
        raw_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        entries = []
    save_json(
        manifest_path,
        {
            "enabled": True,
            "candidate_count": len(candidates),
            "entry_count": len(entries),
            "candidates": candidates,
            "entries": [asdict(entry) for entry in entries],
        },
    )
    return entries


__all__ = [
    "AUTO_GLOSSARY_FILE_NAME",
    "auto_glossary_enabled",
    "extract_auto_glossary_candidates",
    "generate_auto_glossary",
]
