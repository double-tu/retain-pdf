from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from services.translation.llm import translate_batch
from services.translation.llm.control_context import TimeoutPolicy
from services.translation.ocr.json_extractor import get_page_count
from services.translation.ocr.json_extractor import load_ocr_json
from services.translation.diagnostics import TranslationRunDiagnostics
from services.translation.diagnostics import aggregate_payload_diagnostics
from services.translation.diagnostics import classify_provider_family
from services.translation.diagnostics import translation_run_diagnostics_scope
from services.translation.llm.placeholder_guard import should_force_translate_body_text
from services.translation.payload import apply_translated_text_map
from services.translation.payload import pending_translation_items
from services.translation.payload import save_translations
from services.translation.payload import write_translation_manifest
from services.translation.payload.parts.common import clear_translation_fields
from services.translation.payload.parts.common import translation_unit_id
from services.translation.session_context import build_translation_context_from_policy
from services.translation.terms import GlossaryEntry
from services.translation.terms import summarize_glossary_usage
from services.translation.terms.auto_glossary import generate_auto_glossary
from runtime.pipeline.render_mode import resolve_page_range
from services.translation.llm import DEFAULT_BASE_URL
from services.translation.policy import build_book_translation_policy_config
from services.translation.workflow import default_page_translation_name
from runtime.pipeline.book_translation_flow import translate_book_with_global_continuations

RETRYABLE_KEEP_ORIGIN_RECOVERY_ATTEMPTS = 2
TIMEOUT_KEEP_ORIGIN_RECOVERY_ATTEMPTS = 2
TIMEOUT_RECOVERY_PLAIN_TEXT_MIN_SECONDS = 120
TIMEOUT_RECOVERY_BATCH_PLAIN_TEXT_MIN_SECONDS = 150
TIMEOUT_RECOVERY_FORMULA_SEGMENT_MIN_SECONDS = 180
TIMEOUT_RECOVERY_FORMULA_WINDOW_MIN_SECONDS = 210


def _is_retryable_transport_keep_origin(item: dict) -> bool:
    diagnostics = dict(item.get("translation_diagnostics") or {})
    final_status = str(item.get("final_status", "") or diagnostics.get("final_status", "") or "").strip().lower()
    if final_status != "kept_origin":
        return False
    if str(diagnostics.get("fallback_to", "") or "").strip().lower() != "keep_origin":
        return False
    error_trace = diagnostics.get("error_trace") or []
    if any(str((entry or {}).get("type", "") or "").strip().lower() == "transport" for entry in error_trace):
        return True
    degradation_reason = str(diagnostics.get("degradation_reason", "") or "").strip().lower()
    return "transport" in degradation_reason or "timeout" in degradation_reason


def _retryable_keep_origin_unit_ids(translated_pages_map: dict[int, list[dict]]) -> set[str]:
    unit_ids: set[str] = set()
    for items in translated_pages_map.values():
        for item in items:
            if not _is_retryable_transport_keep_origin(item):
                continue
            if not should_force_translate_body_text(item):
                continue
            unit_ids.add(translation_unit_id(item))
    return unit_ids


def _timeout_keep_origin_unit_ids(translated_pages_map: dict[int, list[dict]]) -> set[str]:
    unit_ids: set[str] = set()
    for items in translated_pages_map.values():
        for item in items:
            if not _is_timeout_budget_keep_origin(item):
                continue
            if not should_force_translate_body_text(item):
                continue
            unit_ids.add(translation_unit_id(item))
    return unit_ids


def _collect_retryable_keep_origin_items(translated_pages_map: dict[int, list[dict]]) -> list[dict[str, object]]:
    degraded: list[dict[str, object]] = []
    for page_idx, items in sorted(translated_pages_map.items()):
        for item in items:
            if not _is_retryable_transport_keep_origin(item):
                continue
            if not should_force_translate_body_text(item):
                continue
            diagnostics = dict(item.get("translation_diagnostics") or {})
            degraded.append(
                {
                    "page_idx": page_idx,
                    "page_number": page_idx + 1,
                    "item_id": str(item.get("item_id", "") or ""),
                    "degradation_reason": str(diagnostics.get("degradation_reason", "") or ""),
                }
            )
    return degraded


def _is_timeout_budget_keep_origin(item: dict) -> bool:
    if not _is_retryable_transport_keep_origin(item):
        return False
    diagnostics = dict(item.get("translation_diagnostics") or {})
    degradation_reason = str(diagnostics.get("degradation_reason", "") or "").strip().lower()
    if "timeout" in degradation_reason:
        return True
    error_trace = diagnostics.get("error_trace") or []
    for entry in error_trace:
        entry_dict = entry or {}
        if "timeout" in str(entry_dict.get("code", "") or "").strip().lower():
            return True
        if "timeout" in str(entry_dict.get("message", "") or "").strip().lower():
            return True
    return False


def _reset_retryable_keep_origin_item_for_retry(item: dict) -> None:
    if str(item.get("classification_label", "") or "").strip() == "skip_model_keep_origin":
        item["classification_label"] = ""
    item["should_translate"] = True
    item["skip_reason"] = ""
    item["final_status"] = ""
    item["translation_diagnostics"] = {}
    clear_translation_fields(item)


def _build_timeout_recovery_policy(timeout_policy: TimeoutPolicy | None) -> TimeoutPolicy:
    base_policy = timeout_policy or TimeoutPolicy()
    return TimeoutPolicy(
        plain_text_seconds=max(
            TIMEOUT_RECOVERY_PLAIN_TEXT_MIN_SECONDS,
            int(base_policy.plain_text_seconds) * 2,
        ),
        batch_plain_text_seconds=max(
            TIMEOUT_RECOVERY_BATCH_PLAIN_TEXT_MIN_SECONDS,
            int(base_policy.batch_plain_text_seconds) * 2,
        ),
        formula_segment_seconds=max(
            TIMEOUT_RECOVERY_FORMULA_SEGMENT_MIN_SECONDS,
            int(base_policy.formula_segment_seconds) * 2,
        ),
        formula_window_seconds=max(
            TIMEOUT_RECOVERY_FORMULA_WINDOW_MIN_SECONDS,
            int(base_policy.formula_window_seconds) * 2,
        ),
    )


def _build_timeout_recovery_context(translation_context):
    if translation_context is None:
        return None
    return replace(
        translation_context,
        timeout_policy=_build_timeout_recovery_policy(translation_context.timeout_policy),
    )


def _retry_keep_origin_items(
    *,
    translated_pages_map: dict[int, list[dict]],
    output_dir: Path,
    api_key: str,
    model: str,
    base_url: str,
    mode: str,
    translation_context,
    attempts: int,
    request_prefix: str,
    retry_unit_ids_fn: Callable[[dict[int, list[dict]]], set[str]],
) -> dict[str, int]:
    flat_payload = [
        item
        for page_idx in sorted(translated_pages_map)
        for item in translated_pages_map[page_idx]
    ]
    if not flat_payload:
        return {"retried_units": 0, "recovered_units": 0, "remaining_units": 0}

    unit_to_pages: dict[str, set[int]] = {}
    for page_idx, items in translated_pages_map.items():
        for item in items:
            unit_to_pages.setdefault(translation_unit_id(item), set()).add(page_idx)

    total_retried_units = 0
    total_recovered_units = 0
    for attempt in range(1, attempts + 1):
        retry_unit_ids = retry_unit_ids_fn(translated_pages_map)
        if not retry_unit_ids:
            break
        before_count = len(retry_unit_ids)
        print(
            f"book: {request_prefix} units={before_count} attempt={attempt}/{attempts}",
            flush=True,
        )
        for item in flat_payload:
            if translation_unit_id(item) in retry_unit_ids:
                _reset_retryable_keep_origin_item_for_retry(item)
        retry_units = [
            unit
            for unit in pending_translation_items(flat_payload)
            if translation_unit_id(unit) in retry_unit_ids
        ]
        dirty_pages: set[int] = set()
        for index, unit in enumerate(retry_units, start=1):
            unit_id = translation_unit_id(unit)
            result = translate_batch(
                [unit],
                api_key=api_key,
                model=model,
                base_url=base_url,
                request_label=f"book: {request_prefix} {attempt}/{attempts} item {index}/{len(retry_units)} {unit_id}",
                domain_guidance=translation_context.merged_guidance if translation_context is not None else "",
                mode=mode,
                context=translation_context,
            )
            apply_translated_text_map(flat_payload, result)
            dirty_pages.update(unit_to_pages.get(unit_id, set()))
        for page_idx in sorted(dirty_pages):
            save_translations(
                output_dir / default_page_translation_name(page_idx),
                translated_pages_map[page_idx],
            )
        after_count = len(retry_unit_ids_fn(translated_pages_map))
        total_retried_units += len(retry_units)
        total_recovered_units += max(0, before_count - after_count)
        print(
            f"book: {request_prefix} attempt {attempt} recovered={max(0, before_count - after_count)} remaining={after_count}",
            flush=True,
        )
        if after_count <= 0:
            break
    return {
        "retried_units": total_retried_units,
        "recovered_units": total_recovered_units,
        "remaining_units": len(retry_unit_ids_fn(translated_pages_map)),
    }


def _retry_retryable_keep_origin_items(
    *,
    translated_pages_map: dict[int, list[dict]],
    output_dir: Path,
    api_key: str,
    model: str,
    base_url: str,
    mode: str,
    translation_context,
) -> dict[str, int]:
    return _retry_keep_origin_items(
        translated_pages_map=translated_pages_map,
        output_dir=output_dir,
        api_key=api_key,
        model=model,
        base_url=base_url,
        mode=mode,
        translation_context=translation_context,
        attempts=RETRYABLE_KEEP_ORIGIN_RECOVERY_ATTEMPTS,
        request_prefix="recovery retryable keep_origin",
        retry_unit_ids_fn=_retryable_keep_origin_unit_ids,
    )


def _retry_timeout_budget_keep_origin_items(
    *,
    translated_pages_map: dict[int, list[dict]],
    output_dir: Path,
    api_key: str,
    model: str,
    base_url: str,
    mode: str,
    translation_context,
) -> dict[str, int]:
    timeout_recovery_context = _build_timeout_recovery_context(translation_context)
    if timeout_recovery_context is not None:
        timeout_policy = timeout_recovery_context.timeout_policy
        print(
            "book: timeout recovery policy "
            f"plain={timeout_policy.plain_text_seconds}s "
            f"batch={timeout_policy.batch_plain_text_seconds}s "
            f"formula_segment={timeout_policy.formula_segment_seconds}s "
            f"formula_window={timeout_policy.formula_window_seconds}s",
            flush=True,
        )
    return _retry_keep_origin_items(
        translated_pages_map=translated_pages_map,
        output_dir=output_dir,
        api_key=api_key,
        model=model,
        base_url=base_url,
        mode=mode,
        translation_context=timeout_recovery_context,
        attempts=TIMEOUT_KEEP_ORIGIN_RECOVERY_ATTEMPTS,
        request_prefix="timeout recovery keep_origin",
        retry_unit_ids_fn=_timeout_keep_origin_unit_ids,
    )


def _raise_on_retryable_keep_origin_items(translated_pages_map: dict[int, list[dict]]) -> None:
    degraded = _collect_retryable_keep_origin_items(translated_pages_map)
    if not degraded:
        return
    examples = ", ".join(
        f"p{entry['page_number']}:{entry['item_id']}[{entry['degradation_reason'] or 'keep_origin'}]"
        for entry in degraded[:8]
    )
    raise RuntimeError(
        "Translation stage aborted: "
        f"{len(degraded)} body-text item(s) were kept in the source language after retryable transport failures. "
        "This would leak untranslated text into the rendered PDF. "
        f"Examples: {examples}"
    )


def translate_book_pipeline(
    *,
    source_json_path: Path,
    output_dir: Path,
    api_key: str,
    start_page: int = 0,
    end_page: int = -1,
    batch_size: int = 8,
    workers: int = 1,
    mode: str = "fast",
    math_mode: str = "placeholder",
    classify_batch_size: int = 12,
    skip_title_translation: bool = False,
    model: str = "deepseek-chat",
    base_url: str = DEFAULT_BASE_URL,
    source_pdf_path: Path | None = None,
    rule_profile_name: str = "general_sci",
    custom_rules_text: str = "",
    glossary_id: str = "",
    glossary_name: str = "",
    glossary_resource_entry_count: int = 0,
    glossary_inline_entry_count: int = 0,
    glossary_overridden_entry_count: int = 0,
    glossary_entries: list[GlossaryEntry] | None = None,
    domain_context_pages: int | None = None,
    domain_context_max_chars: int | None = None,
    local_context_neighbors: int | None = None,
    local_context_chars: int | None = None,
    auto_glossary_enabled: bool | None = None,
    auto_glossary_candidates: int | None = None,
    auto_glossary_terms: int | None = None,
    plain_text_timeout_seconds: int | None = None,
    batch_plain_text_timeout_seconds: int | None = None,
    formula_segment_timeout_seconds: int | None = None,
    formula_window_timeout_seconds: int | None = None,
    invocation: dict | None = None,
) -> dict:
    data = load_ocr_json(source_json_path)
    page_count = get_page_count(data)
    if not page_count:
        raise RuntimeError("No pages found in OCR JSON.")

    start, stop = resolve_page_range(page_count, start_page, end_page)
    page_indices = range(start, stop + 1)
    policy_config = build_book_translation_policy_config(
        data=data,
        mode=mode,
        math_mode=math_mode,
        skip_title_translation=skip_title_translation,
        source_pdf_path=source_pdf_path,
        api_key=api_key,
        model=model,
        base_url=base_url,
        output_dir=output_dir,
        rule_profile_name=rule_profile_name,
        custom_rules_text=custom_rules_text,
        domain_context_pages=domain_context_pages,
        domain_context_max_chars=domain_context_max_chars,
    )
    if policy_config.domain_context.get("domain") or policy_config.domain_context.get("translation_guidance"):
        print(
            f"sci domain: {policy_config.domain_context.get('domain', '').strip() or 'unknown'}",
            flush=True,
        )
    print(f"rule profile: {policy_config.rule_profile_name}", flush=True)
    user_glossary_entries = list(glossary_entries or [])
    active_glossary_entries = list(user_glossary_entries)
    timeout_policy = TimeoutPolicy(
        plain_text_seconds=max(10, int(plain_text_timeout_seconds)) if plain_text_timeout_seconds is not None else 35,
        batch_plain_text_seconds=max(10, int(batch_plain_text_timeout_seconds)) if batch_plain_text_timeout_seconds is not None else 45,
        formula_segment_seconds=max(10, int(formula_segment_timeout_seconds)) if formula_segment_timeout_seconds is not None else 60,
        formula_window_seconds=max(10, int(formula_window_timeout_seconds)) if formula_window_timeout_seconds is not None else 75,
    )
    translation_context = build_translation_context_from_policy(
        policy_config,
        glossary_entries=active_glossary_entries,
        model=model,
        base_url=base_url,
        timeout_policy=timeout_policy,
    )
    run_diagnostics = TranslationRunDiagnostics(
        provider_family=classify_provider_family(base_url=base_url, model=model),
        model=model,
        base_url=base_url,
        configured_workers=max(1, workers),
        configured_batch_size=max(1, batch_size),
        configured_classify_batch_size=max(1, classify_batch_size),
    )
    run_diagnostics.set_effective_settings(
        translation_workers=max(1, workers),
        policy_workers=max(1, workers),
        continuation_workers=min(max(1, workers), 8),
        mixed_split_workers=min(max(1, workers), 4),
        translation_batch_size=max(1, min(max(1, batch_size), translation_context.batch_policy.plain_batch_size)),
    )
    with translation_run_diagnostics_scope(run_diagnostics):
        def _prepare_auto_glossary(page_payloads: dict[int, list[dict]]):
            nonlocal active_glossary_entries, translation_context
            auto_entries = generate_auto_glossary(
                page_payloads=page_payloads,
                output_dir=output_dir,
                api_key=api_key,
                model=model,
                base_url=base_url,
                domain_guidance=translation_context.merged_guidance,
                document_summary=policy_config.domain_context.get("summary", ""),
                existing_entries=user_glossary_entries,
                enabled=auto_glossary_enabled,
                max_candidates=auto_glossary_candidates,
                max_terms=auto_glossary_terms,
            )
            if not auto_entries:
                return None
            active_glossary_entries = [*user_glossary_entries, *auto_entries]
            translation_context = build_translation_context_from_policy(
                policy_config,
                glossary_entries=active_glossary_entries,
                model=model,
                base_url=base_url,
                timeout_policy=timeout_policy,
            )
            print(f"auto-glossary: selected {len(auto_entries)} terms", flush=True)
            return translation_context

        translated_pages_map, summaries = translate_book_with_global_continuations(
            data=data,
            output_dir=output_dir,
            page_indices=page_indices,
            api_key=api_key,
            batch_size=batch_size,
            workers=max(1, workers),
            model=model,
            base_url=base_url,
            mode=mode,
            classify_batch_size=max(1, classify_batch_size),
            skip_title_translation=skip_title_translation,
            sci_cutoff_page_idx=policy_config.sci_cutoff_page_idx,
            sci_cutoff_block_idx=policy_config.sci_cutoff_block_idx,
            policy_config=policy_config,
            domain_guidance=policy_config.domain_guidance,
            translation_context=translation_context,
            run_diagnostics=run_diagnostics,
            pre_translation_hook=_prepare_auto_glossary,
            local_context_neighbors=local_context_neighbors,
            local_context_chars=local_context_chars,
        )
    total_items = sum(item["total_items"] for item in summaries)
    translated_items = sum(item["translated_items"] for item in summaries)
    recovery_summary = _retry_retryable_keep_origin_items(
        translated_pages_map=translated_pages_map,
        output_dir=output_dir,
        api_key=api_key,
        model=model,
        base_url=base_url,
        mode=mode,
        translation_context=translation_context,
    )
    timeout_recovery_summary = _retry_timeout_budget_keep_origin_items(
        translated_pages_map=translated_pages_map,
        output_dir=output_dir,
        api_key=api_key,
        model=model,
        base_url=base_url,
        mode=mode,
        translation_context=translation_context,
    )
    total_recovery_retried = recovery_summary["retried_units"] + timeout_recovery_summary["retried_units"]
    total_recovery_recovered = recovery_summary["recovered_units"] + timeout_recovery_summary["recovered_units"]
    total_recovery_remaining = len(_retryable_keep_origin_unit_ids(translated_pages_map))
    if total_recovery_retried > 0:
        print(
            "book: recovery summary "
            f"retried={total_recovery_retried} "
            f"recovered={total_recovery_recovered} "
            f"remaining={total_recovery_remaining} "
            f"(generic={recovery_summary['recovered_units']}, timeout={timeout_recovery_summary['recovered_units']})",
            flush=True,
        )
    _raise_on_retryable_keep_origin_items(translated_pages_map)
    glossary_summary = summarize_glossary_usage(
        entries=active_glossary_entries,
        translated_pages_map=translated_pages_map,
        glossary_id=glossary_id,
        glossary_name=glossary_name,
        resource_entry_count=glossary_resource_entry_count,
        inline_entry_count=glossary_inline_entry_count,
        overridden_entry_count=glossary_overridden_entry_count,
    )
    _, diagnostics_summary = aggregate_payload_diagnostics(translated_pages_map)
    write_translation_manifest(
        output_dir,
        {
            page_idx: output_dir / default_page_translation_name(page_idx)
            for page_idx in translated_pages_map
        },
        glossary=glossary_summary,
        summary={
            "math_mode": math_mode,
            **diagnostics_summary,
            **({"invocation": invocation} if invocation else {}),
        },
    )
    return {
        "output_dir": output_dir,
        "start_page": start,
        "end_page": stop,
        "page_count": len(summaries),
        "total_items": total_items,
        "translated_items": translated_items,
        "translated_pages_map": translated_pages_map,
        "summaries": summaries,
        "domain_context": policy_config.domain_context,
        "rule_profile_name": policy_config.rule_profile_name,
        "custom_rules_text": policy_config.custom_rules_text,
        "glossary": glossary_summary,
        "diagnostics_summary": diagnostics_summary,
        "invocation": invocation or {},
        "math_mode": math_mode,
        "translation_context": translation_context,
        "translation_run_diagnostics": run_diagnostics,
    }
