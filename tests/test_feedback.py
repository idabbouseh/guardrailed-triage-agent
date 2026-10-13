"""Tests for record_human_decision in triage_agent/tools.py.

Covers validation, the kill-switch path, audit-line content (including card
redaction), and the guarantee that no helpdesk store or preview registry is
touched.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mock_helpdesk import data
from mock_helpdesk.app import app
from triage_agent import api_client, guard, tools

GUARD_ENV_VARS = ("AGENT_HALT", "HALT_FILE_PATH", "MAX_CREDIT_USD", "AUDIT_LOG_PATH")


@pytest.fixture(autouse=True)
def feedback_env(monkeypatch, tmp_path):
    """Fresh registry, temp audit log, temp HALT path, in-process mock client."""
    for name in GUARD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("HALT_FILE_PATH", str(tmp_path / "HALT"))
    guard.set_registry(guard.PreviewRegistry())
    api_client.set_client(api_client.HelpdeskClient(http_client=TestClient(app)))
    yield SimpleNamespace(audit_path=audit_path)
    api_client.set_client(None)
    guard.set_registry(None)


def _phases():
    return [r["phase"] for r in guard.read_audit()]


# --- validation errors ---------------------------------------------------


def test_invalid_ticket_id_returns_error():
    # Arrange / Act
    result = tools.record_human_decision("", "rejected", "not applicable")

    # Assert
    assert result.get("error") is True and result.get("invalid") is True
    assert _phases() == []


def test_invalid_decision_returns_error():
    # Act
    result = tools.record_human_decision("T-2006", "approved", "looks good")

    # Assert
    assert result.get("error") is True and result.get("invalid") is True
    assert "rejected" in result["detail"] or "edited" in result["detail"]
    assert _phases() == []


def test_empty_reason_returns_error():
    # Act
    result = tools.record_human_decision("T-2006", "rejected", "   ")

    # Assert
    assert result.get("error") is True and result.get("invalid") is True
    assert "reason" in result["detail"]
    assert _phases() == []


def test_invalid_proposed_tool_returns_error():
    # Act
    result = tools.record_human_decision("T-2006", "rejected", "wrong tool", proposed_tool="summarize")

    # Assert
    assert result.get("error") is True and result.get("invalid") is True
    assert _phases() == []


def test_edited_without_edited_text_returns_error():
    # "edited" decision requires edited_text
    result = tools.record_human_decision("T-2006", "edited", "I changed my mind")

    # Assert
    assert result.get("error") is True and result.get("invalid") is True
    assert "edited_text" in result["detail"]
    assert _phases() == []


def test_edited_with_blank_edited_text_returns_error():
    result = tools.record_human_decision("T-2006", "edited", "I changed my mind", edited_text="   ")

    assert result.get("error") is True and result.get("invalid") is True
    assert _phases() == []


# --- halted path ---------------------------------------------------------


def test_halted_returns_halted_dict_and_no_human_audit_phase(monkeypatch, feedback_env):
    # Arrange: flip the kill switch
    monkeypatch.setenv("AGENT_HALT", "1")

    # Act
    result = tools.record_human_decision("T-2006", "rejected", "wrong tool")

    # Assert
    assert result.get("halted") is True and result.get("error") is True
    phases = _phases()
    assert "human_rejected" not in phases
    assert "human_edited" not in phases


# --- audit trail ---------------------------------------------------------


def test_rejected_writes_human_rejected_phase_and_returns_recorded(feedback_env):
    # Act
    result = tools.record_human_decision(
        "T-2006", "rejected", "not the right action", proposed_tool="draft_reply"
    )

    # Assert
    assert result == {"recorded": True, "ticket_id": "T-2006", "decision": "rejected"}
    records = guard.read_audit()
    assert len(records) == 1
    assert records[0]["phase"] == "human_rejected"
    assert records[0]["payload"]["ticket_id"] == "T-2006"
    assert records[0]["payload"]["proposed_tool"] == "draft_reply"
    assert records[0]["payload"]["reason"] == "not the right action"


def test_edited_writes_human_edited_phase_and_returns_recorded(feedback_env):
    # Act
    result = tools.record_human_decision(
        "T-2007", "edited", "amount is too low",
        proposed_tool="issue_credit",
        edited_text="Please make the credit seventy five dollars instead.",
    )

    # Assert
    assert result == {"recorded": True, "ticket_id": "T-2007", "decision": "edited"}
    records = guard.read_audit()
    assert records[0]["phase"] == "human_edited"
    assert records[0]["payload"]["edited_text"] == "Please make the credit seventy five dollars instead."


def test_card_number_in_reason_is_redacted_in_audit(feedback_env):
    # Arrange: the human's reason happens to contain a PAN
    pan = "4111111111111111"

    # Act
    result = tools.record_human_decision("T-2006", "rejected", f"the card {pan} was wrong")

    # Assert
    assert result["recorded"] is True
    records = guard.read_audit()
    assert pan not in json.dumps(records)
    assert "****" in json.dumps(records[0]["payload"])


def test_empty_proposed_tool_is_accepted_for_rejected(feedback_env):
    # No proposed_tool is valid; forbidden_actions stays empty in the harvest
    result = tools.record_human_decision("T-2006", "rejected", "nothing seemed right")
    assert result["recorded"] is True
    assert _phases() == ["human_rejected"]


# --- helpdesk stores and registry are untouched --------------------------


def test_rejected_does_not_touch_helpdesk_stores(feedback_env):
    # Arrange: snapshot the stores
    replies_before = list(data.REPLIES)
    tasks_before = list(data.TASKS)
    credits_before = list(data.CREDITS)
    escalations_before = [t for t in data.TICKETS if t.get("status") == "escalated"]

    # Act
    tools.record_human_decision("T-2006", "rejected", "wrong tool")

    # Assert: stores unchanged
    assert data.REPLIES == replies_before
    assert data.TASKS == tasks_before
    assert data.CREDITS == credits_before
    assert [t for t in data.TICKETS if t.get("status") == "escalated"] == escalations_before


def test_record_human_decision_does_not_consume_existing_preview(feedback_env):
    # Arrange: preview a write tool, then record a rejection
    tools.draft_reply("T-2003", "Hello there.", "scheduling")
    tools.record_human_decision("T-2003", "rejected", "too short")

    # Act: the preview should still be consumable
    confirm = tools.draft_reply("T-2003", "Hello there.", "scheduling", confirmed=True)

    # Assert: confirm succeeded, meaning record_human_decision did not touch the registry
    assert confirm.get("reply_id") is not None


# --- regression: Finding F-01 (instruction security) -----------------------


def test_instruction_operating_contract_excludes_data_sources_as_human_rejection():
    """Regression F-01: OPERATING CONTRACT must explicitly tell the model that
    ticket content, tool results and other data sources are never a human
    rejection or edit.  Prompt-injected ticket text must not cause the model
    to call record_human_decision autonomously."""
    from triage_agent.agent import INSTRUCTION

    text = INSTRUCTION.lower()
    # The new sentence uses "data source" to cover tickets, tool results, etc.
    assert "data source" in text, (
        "INSTRUCTION OPERATING CONTRACT must state that data sources are not "
        "human rejections or edits"
    )


def test_record_human_decision_docstring_warns_against_ticket_content():
    """Regression F-01: The model-facing docstring must explicitly forbid calling
    record_human_decision in response to ticket content or tool results."""
    import inspect

    doc = (inspect.getdoc(tools.record_human_decision) or "").lower()
    assert "ticket content" in doc or "tool result" in doc, (
        "record_human_decision docstring must warn the model not to call it "
        "based on ticket content or tool results"
    )
