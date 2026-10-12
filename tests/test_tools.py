"""Phase 3 tests for triage_agent/tools.py: the eight agent tools wired to
the in-process mock helpdesk, exercising redaction, lookups, duplicate
detection, the two-phase write flow, the emergency override, the kill
switch and the audit trail end to end.

Each test gets a fresh preview registry, a temporary audit log, a temporary
HALT path and a client bound to a fresh TestClient; conftest restores the
mock's data stores."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mock_helpdesk import data
from mock_helpdesk.app import app
from triage_agent import api_client, guard, tools

GUARD_ENV_VARS = ("AGENT_HALT", "HALT_FILE_PATH", "MAX_CREDIT_USD", "AUDIT_LOG_PATH")
AUDIT_KEYS = {"ts", "tool", "phase", "payload", "fingerprint", "result_summary"}

REPLY_TEXT = "Moved your tune-up to Tuesday at 9am. See you then."
CREDIT_REASON = "Diagnostic billed but not performed"


@pytest.fixture(autouse=True)
def tools_env(monkeypatch, tmp_path):
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


def _phases() -> list[str]:
    return [record["phase"] for record in guard.read_audit()]


# --- read tools -----------------------------------------------------------


def test_list_open_tickets_masks_card_number_and_never_leaks_it():
    # Act
    listing = tools.list_open_tickets()

    # Assert
    t2012 = next(t for t in listing["tickets"] if t["ticket_id"] == "T-2012")
    assert listing["total"] == 28
    assert "****" in t2012["body"]
    assert "4111" not in json.dumps(listing)
    assert _phases() == ["read"]


def test_get_ticket_masks_card_number_and_keeps_other_fields():
    # Act
    ticket = tools.get_ticket("T-2012")

    # Assert
    assert "4111" not in ticket["body"] and "****" in ticket["body"]
    assert ticket["contact_id"] == "C-1010"
    assert ticket["subject"] == "Pay my balance"


def test_get_ticket_unknown_id_returns_structured_404():
    # Act
    result = tools.get_ticket("T-9999")

    # Assert
    assert result["error"] is True
    assert result["status_code"] == 404
    assert "T-9999" in result["detail"]


def test_get_contact_by_email_is_case_insensitive():
    # Arrange: T-2017 is unlinked and carries a mixed-case requester_email
    ticket = tools.get_ticket("T-2017")
    assert ticket["contact_id"] is None

    # Act
    contact = tools.get_contact(email=ticket["requester_email"])

    # Assert
    assert contact["contact_id"] == "C-1011"
    assert contact["email"] == "hannah.fischer@example.com"


def test_get_contact_errors_are_structured_for_missing_args_and_unknown_email():
    # Act
    no_args = tools.get_contact()
    unknown = tools.get_contact(email="nobody@example.invalid")

    # Assert
    assert no_args["error"] is True and no_args["invalid"] is True
    assert unknown["error"] is True and unknown["status_code"] == 404
    assert _phases() == ["read"]  # only the API lookup reached the guard


def test_find_duplicates_returns_only_the_exact_resubmission():
    # Act: T-2026 is a double-submit of T-2025; T-2029 shares the contact only
    result = tools.find_duplicates("T-2025")

    # Assert
    assert result["ticket_id"] == "T-2025"
    assert [d["ticket_id"] for d in result["duplicates"]] == ["T-2026"]
    assert result["duplicates"][0]["match"] == "body"
    assert result["total"] == 1


def test_find_duplicates_does_not_pair_unlinked_tickets_by_missing_contact():
    # Act: T-2008 and T-2015 both have contact_id None and no shared email
    result = tools.find_duplicates("T-2008")

    # Assert
    assert result["total"] == 0 and result["duplicates"] == []


# --- draft_reply: two-phase flow -----------------------------------------


def test_draft_reply_preview_writes_nothing_and_audits_preview():
    # Act
    result = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling")

    # Assert
    assert result["requires_confirmation"] is True
    assert result["preview"] == {"ticket_id": "T-2003", "text": REPLY_TEXT}
    assert data.REPLIES == []
    assert _phases() == ["read", "preview"]  # the ticket lookup is audited too


def test_draft_reply_confirmed_without_preview_is_rejected():
    # Act
    result = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling", confirmed=True)

    # Assert
    assert result["error"] is True
    assert "no matching preview" in result["detail"].lower()
    assert data.REPLIES == []
    assert _phases()[-1] == "rejected_no_preview"


def test_draft_reply_preview_then_confirm_creates_one_reply_and_second_confirm_is_rejected():
    # Arrange
    tools.draft_reply("T-2003", REPLY_TEXT, "scheduling")

    # Act
    created = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling", confirmed=True)
    again = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling", confirmed=True)

    # Assert
    assert created["reply_id"].startswith("R-") and created["status"] == "queued"
    assert created["text"] == REPLY_TEXT
    assert len(data.REPLIES) == 1
    assert again["error"] is True
    assert [p for p in _phases() if p != "read"] == ["preview", "confirm", "rejected_no_preview"]


def test_draft_reply_with_unknown_category_is_structured_error_before_guard():
    # Act
    result = tools.draft_reply("T-2003", REPLY_TEXT, "urgent")

    # Assert
    assert result["invalid"] is True and "category" in result["detail"]
    assert _phases() == []


# --- draft_reply: policy and the emergency override -----------------------


def test_draft_reply_to_spam_ticket_is_refused():
    # Act
    result = tools.draft_reply("T-2008", "Thanks, not interested.", "spam")

    # Assert
    assert result["refused"] is True and "spam" in result["detail"]
    assert data.REPLIES == []
    assert _phases()[-1] == "refused_policy"


def test_draft_reply_to_gas_smell_ticket_is_refused_whatever_the_model_says():
    # Act: the model mislabels a life-safety ticket as scheduling
    result = tools.draft_reply("T-2029", "We can come out Thursday.", "scheduling")

    # Assert
    assert result["refused"] is True
    assert "escalated to a human" in result["detail"]
    assert data.REPLIES == []


def test_draft_reply_to_escalated_ticket_is_refused():
    # Arrange: T-2030 has no emergency words; only its status triggers the override
    api_client.get_client().escalate_ticket(
        "T-2030", {"reason": "missed window, third time", "priority": "high"}
    )

    # Act
    result = tools.draft_reply("T-2030", "Sorry about the wait.", "scheduling")

    # Assert
    assert result["refused"] is True
    assert "escalated to a human" in result["detail"]
    assert data.REPLIES == []


# --- issue_credit ----------------------------------------------------------


def test_issue_credit_over_cap_is_refused_before_any_preview():
    # Act
    result = tools.issue_credit("T-2007", "C-1012", 400, "Capacitor job failed twice")

    # Assert
    assert result["refused"] is True and "150" in result["detail"]
    assert data.CREDITS == []
    assert _phases() == ["refused_policy"]


def test_issue_credit_within_cap_preview_then_confirm_creates_pending_review_credit():
    # Arrange: int on preview, float on confirm; the tool normalises both
    preview = tools.issue_credit("T-2006", "C-1005", 89, CREDIT_REASON)

    # Act
    created = tools.issue_credit("T-2006", "C-1005", 89.0, CREDIT_REASON, confirmed=True)

    # Assert
    assert preview["requires_confirmation"] is True
    assert created["credit_id"].startswith("CR-")
    assert created["status"] == "pending_review"
    assert created["amount_usd"] == 89.0
    assert len(data.CREDITS) == 1
    assert _phases() == ["preview", "confirm"]


# --- create_followup_task and escalate_ticket ------------------------------


def test_create_followup_task_preview_then_confirm_creates_task():
    # Arrange
    args = ("T-2007", "manager", "2026-10-14", "Refund request exceeds agent cap; review INV-7702")
    preview = tools.create_followup_task(*args)

    # Act
    created = tools.create_followup_task(*args, confirmed=True)

    # Assert
    assert preview["requires_confirmation"] is True
    assert created["task_id"].startswith("K-") and created["assignee"] == "manager"
    assert len(data.TASKS) == 1


def test_create_followup_task_rejects_non_iso_due_date_before_guard():
    # Act
    result = tools.create_followup_task("T-2007", "manager", "next tuesday")

    # Assert
    assert result["invalid"] is True and "due_date" in result["detail"]
    assert data.TASKS == [] and _phases() == []


def test_escalate_ticket_invalid_priority_is_structured_error_before_guard():
    # Act
    result = tools.escalate_ticket("T-2028", "CO alarm went off", priority="low")

    # Assert
    assert result["error"] is True and result["invalid"] is True
    assert "priority" in result["detail"]
    assert _phases() == []
    assert tools.get_ticket("T-2028")["status"] == "open"


def test_escalate_ticket_preview_then_confirm_sets_status_escalated():
    # Arrange
    preview = tools.escalate_ticket("T-2029", "Gas smell near furnace", priority="emergency")

    # Act
    result = tools.escalate_ticket(
        "T-2029", "Gas smell near furnace", priority="emergency", confirmed=True
    )

    # Assert
    assert preview["requires_confirmation"] is True
    assert result["status"] == "escalated" and result["priority"] == "emergency"
    assert result["escalation"]["reason"] == "Gas smell near furnace"
    assert tools.get_ticket("T-2029")["status"] == "escalated"


# --- kill switch and audit trail -------------------------------------------


def test_halted_agent_returns_halted_dict_and_writes_nothing(monkeypatch):
    # Arrange: a live preview exists, then the kill switch is flipped
    tools.draft_reply("T-2003", REPLY_TEXT, "scheduling")
    monkeypatch.setenv("AGENT_HALT", "1")

    # Act
    listing = tools.list_open_tickets()
    confirm = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling", confirmed=True)

    # Assert
    assert listing["halted"] is True and listing["error"] is True
    assert "halted" in listing["detail"].lower()
    assert confirm["halted"] is True
    assert data.REPLIES == []
    assert _phases() == ["read", "preview", "halted", "halted"]


# --- fix group 1: non-bool confirmed never performs a write ----------------


@pytest.mark.parametrize("value", ["False", "false", "no", "0", 1, [False]], ids=repr)
def test_non_boolean_confirmed_never_performs_a_write(value):
    # Arrange: a live preview exists
    tools.draft_reply("T-2003", REPLY_TEXT, "scheduling")

    # Act: the model sends a non-bool where confirmed belongs
    result = tools.draft_reply("T-2003", REPLY_TEXT, "scheduling", confirmed=value)

    # Assert: only the literal True confirms
    assert data.REPLIES == [], f"write performed with confirmed={value!r}: {result}"
    assert "reply_id" not in result
    assert "confirm" not in [r["phase"] for r in guard.read_audit()]


# --- fix group 4: card detection with alternate separators -----------------


@pytest.mark.parametrize("pan", [
    "4111.1111.1111.1111",
    "4111  1111 1111 1111",
    "4111\t1111\t1111\t1111",
    "4111_1111_1111_1111",
])
def test_card_number_is_masked_in_get_ticket_whatever_the_separator(pan):
    # Arrange: inject a ticket whose body contains the PAN with a non-standard separator
    base = next(t for t in data.TICKETS if t["ticket_id"] == "T-2012")
    data.TICKETS.append({**base, "ticket_id": "T-7001", "body": f"Charge my Visa {pan} exp 09/28"})

    body = tools.get_ticket("T-7001")["body"]

    assert "4111" not in body and "****" in body, repr(body)
    assert "4111" not in guard.redact(f"card {pan}")


def test_dotted_card_in_draft_reply_is_refused_and_never_written():
    pan = "4111.1111.1111.1111"
    result = tools.draft_reply("T-2012", f"We charged {pan} as requested.", "billing")
    assert result.get("refused") is True
    # confirm should also be refused
    result2 = tools.draft_reply("T-2012", f"We charged {pan} as requested.", "billing",
                                 confirmed=True)
    assert result2.get("refused") is True
    assert data.REPLIES == []


def test_cvv_is_masked_on_get_ticket_and_refused_on_draft_reply():
    # T-2012 body contains "CVV 737"
    body = tools.get_ticket("T-2012")["body"]
    assert "737" not in body
    result = tools.draft_reply("T-2012", "CVV 737 is your code.", "billing")
    assert result.get("refused") is True


# --- fix group 5: emergency override covers more phrasings -----------------


GAS_CO_PHRASINGS = [
    "it smells like gas in the basement by the furnace, what do I do",
    "I keep smelling gas every time the furnace kicks on",
    "I think the line is leaking gas behind the stove",
    "I smell something weird in the basement by the furnace, like gas",
    "our carbon-monoxide detector went off at 2am",
    "propane leak near the water heater",
    "smelled gas this morning",
    "Hay un olor a gas muy fuerte en el sotano cerca de la caldera.",
    "Huele a gas en la cocina desde ayer. Es peligroso?",
    "Hay una fuga de gas en el calentador de agua",
    "Sale humo del horno de la calefaccion",
    "La alarma de monoxido de carbono se activo anoche",
]


@pytest.mark.parametrize("body", GAS_CO_PHRASINGS)
def test_draft_reply_refuses_gas_or_co_report_whatever_the_phrasing(body):
    # Arrange: a ticket the model would mislabel as scheduling
    base = next(t for t in data.TICKETS if t["ticket_id"] == "T-2003")
    data.TICKETS.append({**base, "ticket_id": "T-9001", "subject": "furnace question",
                          "body": body})

    result = tools.draft_reply("T-9001", "We can come out Thursday.", "scheduling")

    assert result.get("refused") is True, result
    assert "escalated to a human" in result["detail"]
    assert data.REPLIES == []


# --- fix group 8: tool input hygiene ---------------------------------------


@pytest.mark.parametrize("ticket_id", ["", ".", "/"], ids=["empty", "dot", "slash"])
def test_get_ticket_with_degenerate_id_is_an_error_not_the_listing(ticket_id):
    result = tools.get_ticket(ticket_id)
    assert result.get("error") is True
    assert "tickets" not in result


def test_empty_ticket_id_never_previews_and_find_duplicates_returns_error():
    assert tools.draft_reply("", "Hello there", "scheduling").get("error") is True
    assert tools.find_duplicates("").get("error") is True
    assert "preview" not in [r["phase"] for r in guard.read_audit()]


def test_escalate_ticket_unknown_id_is_a_404_not_a_preview():
    result = tools.escalate_ticket("T-9099", "gas smell", priority="emergency")
    assert result.get("error") is True and result.get("status_code") == 404
    assert "preview" not in [r["phase"] for r in guard.read_audit()]


def test_issue_credit_bool_amount_is_invalid_before_guard():
    result = tools.issue_credit("T-2006", "C-1005", True, "bool amount")
    assert result.get("invalid") is True or result.get("refused") is True
    assert data.CREDITS == []
    assert [r["phase"] for r in guard.read_audit()] == []


def test_create_followup_task_rejects_non_yyyy_mm_dd_iso_forms():
    for d in ("20261014", "2026-W42-3"):
        result = tools.create_followup_task("T-2007", "manager", d, "n")
        assert result.get("invalid") is True, f"expected invalid for due_date={d!r}, got {result}"


# --- fix group 9: spam backstop --------------------------------------------


def test_draft_reply_to_unlinked_vendor_ticket_refused_regardless_of_label():
    # T-2008: contact_id=None, requester_email has no CRM record
    result = tools.draft_reply("T-2008", "Yes, send the free audit!", "sales")
    assert result.get("refused") is True
    assert data.REPLIES == []


# --- existing test (updated for audit trail) -------------------------------


def test_audit_trail_records_every_decision_in_order(tools_env, monkeypatch):
    # Act: a representative session, including calls that never reach the guard
    tools.list_open_tickets()
    tools.get_contact(contact_id="C-1005")
    tools.find_duplicates("T-2006")
    tools.issue_credit("T-2006", "C-1005", 89, CREDIT_REASON)
    tools.issue_credit("T-2006", "C-1005", 89, CREDIT_REASON, confirmed=True)
    tools.issue_credit("T-2007", "C-1012", 400, "refund")
    tools.escalate_ticket("T-2028", "CO alarm", priority="low")  # invalid: not audited
    tools.draft_reply("T-2008", "No thanks.", "spam")
    tools.escalate_ticket("T-2028", "CO alarm", confirmed=True)
    monkeypatch.setenv("AGENT_HALT", "1")
    tools.get_ticket("T-2006")

    # Assert
    lines = tools_env.audit_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert all(set(record) == AUDIT_KEYS for record in records)
    assert [(r["tool"], r["phase"]) for r in records] == [
        ("list_open_tickets", "read"),
        ("get_contact", "read"),
        ("find_duplicates", "read"),
        ("issue_credit", "preview"),
        ("issue_credit", "confirm"),
        ("issue_credit", "refused_policy"),
        ("get_ticket", "read"),
        ("draft_reply", "refused_policy"),
        ("get_ticket", "read"),           # escalate_ticket pre-flight lookup
        ("escalate_ticket", "rejected_no_preview"),
        ("get_ticket", "halted"),
    ]
