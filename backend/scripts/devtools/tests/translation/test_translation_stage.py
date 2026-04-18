import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_SCRIPTS_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_SCRIPTS_ROOT))

from runtime.pipeline.translation_stage import _collect_retryable_keep_origin_items
from runtime.pipeline.translation_stage import _raise_on_retryable_keep_origin_items
from runtime.pipeline.translation_stage import _retry_retryable_keep_origin_items


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
