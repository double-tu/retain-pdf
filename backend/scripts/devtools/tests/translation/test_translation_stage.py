import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_SCRIPTS_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_SCRIPTS_ROOT))

from runtime.pipeline.translation_stage import _collect_retryable_keep_origin_items
from runtime.pipeline.translation_stage import _build_timeout_recovery_context
from runtime.pipeline.translation_stage import _build_timeout_recovery_policy
from runtime.pipeline.translation_stage import _raise_on_retryable_keep_origin_items
from runtime.pipeline.translation_stage import _retry_retryable_keep_origin_items
from runtime.pipeline.translation_stage import _retry_timeout_budget_keep_origin_items
from services.translation.llm.control_context import TimeoutPolicy
from services.translation.llm.control_context import build_translation_control_context


def _item(item_id: str, source_text: str, **overrides):
    item = {
        "item_id": item_id,
        "page_idx": 0,
        "block_type": "text",
        "source_text": source_text,
        "protected_source_text": source_text,
        "metadata": {"structure_role": "body"},
        "final_status": "kept_origin",
        "translation_diagnostics": {
            "fallback_to": "keep_origin",
            "degradation_reason": "transport_timeout_budget_exceeded",
            "final_status": "kept_origin",
            "error_trace": [{"type": "transport", "code": "TRANSPORT_ERROR"}],
        },
    }
    item.update(overrides)
    return item


def test_collect_retryable_keep_origin_items_only_flags_body_transport_degradation() -> None:
    body_item = _item(
        "p001-b001",
        "This paragraph contains enough English body text to require translation and should not stay untranslated.",
    )
    caption_item = _item(
        "p001-b002",
        "Figure 1",
        metadata={"structure_role": "caption"},
    )
    validation_item = _item(
        "p001-b003",
        "This paragraph also looks like body text and is long enough to translate safely in the pipeline.",
        translation_diagnostics={
            "fallback_to": "keep_origin",
            "degradation_reason": "english_residue_repeated",
            "final_status": "kept_origin",
            "error_trace": [{"type": "validation", "code": "ENGLISH_RESIDUE"}],
        },
    )

    degraded = _collect_retryable_keep_origin_items(
        {
            0: [body_item, caption_item, validation_item],
        }
    )

    assert degraded == [
        {
            "page_idx": 0,
            "page_number": 1,
            "item_id": "p001-b001",
            "degradation_reason": "transport_timeout_budget_exceeded",
        }
    ]


def test_raise_on_retryable_keep_origin_items_aborts_translation_stage() -> None:
    pages = {
        1: [
            _item(
                "p002-b017",
                "This paragraph discusses catalytic activity in enough detail that the system should never leak it as raw English.",
                page_idx=1,
            )
        ]
    }

    with pytest.raises(RuntimeError, match="kept in the source language"):
        _raise_on_retryable_keep_origin_items(pages)


def test_retry_retryable_keep_origin_items_recovers_and_persists_page_payload(tmp_path: Path) -> None:
    source_text = (
        "This paragraph discusses catalytic activity in enough detail that the system should never leak it as raw English."
    )
    pages = {
        0: [
            _item(
                "p001-b001",
                source_text,
                translation_unit_id="p001-b001",
            )
        ]
    }

    with mock.patch(
        "runtime.pipeline.translation_stage.translate_batch",
        return_value={
            "p001-b001": {
                "decision": "translate",
                "translated_text": "这段文字已经被重新翻译完整。",
                "final_status": "translated",
            }
        },
    ):
        summary = _retry_retryable_keep_origin_items(
            translated_pages_map=pages,
            output_dir=tmp_path,
            api_key="sk-test",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            mode="fast",
            translation_context=None,
        )

    assert summary == {"retried_units": 1, "recovered_units": 1, "remaining_units": 0}
    assert pages[0][0]["translated_text"] == "这段文字已经被重新翻译完整。"
    assert pages[0][0]["final_status"] == "translated"
    persisted = (tmp_path / "page-001-deepseek.json").read_text(encoding="utf-8")
    assert "这段文字已经被重新翻译完整。" in persisted


def test_build_timeout_recovery_policy_expands_timeout_budget() -> None:
    policy = _build_timeout_recovery_policy(
        TimeoutPolicy(
            plain_text_seconds=35,
            batch_plain_text_seconds=45,
            formula_segment_seconds=60,
            formula_window_seconds=75,
        )
    )

    assert policy.plain_text_seconds == 120
    assert policy.batch_plain_text_seconds == 150
    assert policy.formula_segment_seconds == 180
    assert policy.formula_window_seconds == 210


def test_build_timeout_recovery_context_preserves_guidance_and_swaps_timeout_policy() -> None:
    context = build_translation_control_context(
        mode="fast",
        domain_guidance="domain",
        rule_guidance="rule",
        extra_guidance="extra",
        timeout_policy=TimeoutPolicy(
            plain_text_seconds=40,
            batch_plain_text_seconds=50,
            formula_segment_seconds=70,
            formula_window_seconds=80,
        ),
    )

    timeout_context = _build_timeout_recovery_context(context)

    assert timeout_context is not None
    assert timeout_context is not context
    assert timeout_context.domain_guidance == "domain"
    assert timeout_context.rule_guidance == "rule"
    assert timeout_context.extra_guidance == "extra"
    assert timeout_context.timeout_policy.plain_text_seconds == 120
    assert timeout_context.timeout_policy.batch_plain_text_seconds == 150
    assert timeout_context.timeout_policy.formula_segment_seconds == 180
    assert timeout_context.timeout_policy.formula_window_seconds == 210


def test_retry_timeout_budget_keep_origin_items_uses_expanded_timeout_context(tmp_path: Path) -> None:
    source_text = (
        "This paragraph discusses catalytic activity in enough detail that the system should never leak it as raw English."
    )
    timeout_item = _item(
        "p001-b001",
        source_text,
        translation_unit_id="p001-b001",
    )
    non_timeout_item = _item(
        "p001-b002",
        source_text,
        translation_unit_id="p001-b002",
        translation_diagnostics={
            "fallback_to": "keep_origin",
            "degradation_reason": "transport_provider_unavailable",
            "final_status": "kept_origin",
            "error_trace": [{"type": "transport", "code": "TRANSPORT_ERROR"}],
        },
    )
    pages = {0: [timeout_item, non_timeout_item]}
    context = build_translation_control_context(
        mode="fast",
        domain_guidance="domain guidance",
        timeout_policy=TimeoutPolicy(
            plain_text_seconds=35,
            batch_plain_text_seconds=45,
            formula_segment_seconds=60,
            formula_window_seconds=75,
        ),
    )
    observed_contexts = []

    def _fake_translate_batch(batch, **kwargs):
        observed_contexts.append(kwargs.get("context"))
        unit_id = batch[0]["translation_unit_id"]
        return {
            unit_id: {
                "decision": "translate",
                "translated_text": f"{unit_id}-translated",
                "final_status": "translated",
            }
        }

    with mock.patch("runtime.pipeline.translation_stage.translate_batch", side_effect=_fake_translate_batch):
        summary = _retry_timeout_budget_keep_origin_items(
            translated_pages_map=pages,
            output_dir=tmp_path,
            api_key="sk-test",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            mode="fast",
            translation_context=context,
        )

    assert summary == {"retried_units": 1, "recovered_units": 1, "remaining_units": 0}
    assert len(observed_contexts) == 1
    timeout_context = observed_contexts[0]
    assert timeout_context is not None
    assert timeout_context.timeout_policy.plain_text_seconds == 120
    assert timeout_context.timeout_policy.batch_plain_text_seconds == 150
    assert timeout_context.timeout_policy.formula_segment_seconds == 180
    assert timeout_context.timeout_policy.formula_window_seconds == 210
    assert pages[0][0]["translated_text"] == "p001-b001-translated"
    assert pages[0][1]["final_status"] == "kept_origin"
