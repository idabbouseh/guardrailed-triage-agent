"""Tests for triage_agent/guard.py: the kill switch, two-phase previews,
policy caps and the audit trail.

Every test runs against a fresh PreviewRegistry driven by a fake clock, a
temporary audit file and a temporary HALT path, so nothing leaks between
tests or touches the real repo root."""

import json
from types import SimpleNamespace

import pytest

from triage_agent import guard

GUARD_ENV_VARS = (
    "AGENT_HALT",
    "HALT_FILE_PATH",
    "MAX_CREDIT_USD",
    "AUDIT_LOG_PATH",
    "PREVIEW_TTL_SECONDS",
)
AUDIT_KEYS = {"ts", "tool", "phase", "payload", "fingerprint", "result_summary"}

CREDIT = {"ticket_id": "T-1001", "amount_usd": 25.0, "reason": "technician late"}
REPLY = {"ticket_id": "T-1001", "body": "Thanks for your patience."}
ESCALATION = {"ticket_id": "T-1001", "reason": "gas smell reported"}

VISA_TEST_NUMBER = "4111 1111 1111 1111"  # Luhn-valid, publicly documented test PAN
NOT_A_CARD = "1234 5678 9012 3456"  # 16 digits but Luhn-invalid


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """Stand-in for an API call: counts invocations, returns a canned result."""

    def __init__(self, result: dict | None = None):
        self.calls: list[dict] = []
        self.result = result or {"credit_id": "CR-7", "status": "issued"}

    def __call__(self, payload: dict) -> dict:
        self.calls.append(payload)
        return self.result


@pytest.fixture(autouse=True)
def guard_env(monkeypatch, tmp_path):
    for name in GUARD_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    audit_path = tmp_path / "audit.jsonl"
    halt_file = tmp_path / "HALT"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("HALT_FILE_PATH", str(halt_file))
    clock = FakeClock()
    guard.set_registry(guard.PreviewRegistry(now=clock))
    yield SimpleNamespace(clock=clock, audit_path=audit_path, halt_file=halt_file)
    guard.set_registry(None)


def _phases() -> list[str]:
    return [record["phase"] for record in guard.read_audit()]


# --- kill switch -----------------------------------------------------------


def test_not_halted_by_default_or_with_falsey_env(monkeypatch):
    # Arrange: fixture cleared AGENT_HALT and pointed HALT_FILE_PATH at tmp
    assert guard.is_halted() is False

    # Act / Assert: explicit "off" values do not halt either
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv("AGENT_HALT", value)
        assert guard.is_halted() is False, value


def test_halted_by_env_variants_case_insensitively(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "Yes"):
        # Arrange
        monkeypatch.setenv("AGENT_HALT", value)
        # Act / Assert
        assert guard.is_halted() is True, value


def test_halted_by_halt_file_and_released_when_removed(guard_env):
    # Arrange
    assert guard.is_halted() is False
    guard_env.halt_file.write_text("stop")

    # Act / Assert: evaluated per call, never cached
    assert guard.is_halted() is True
    guard_env.halt_file.unlink()
    assert guard.is_halted() is False


def test_default_paths_resolve_relative_to_repo_root(monkeypatch):
    # Arrange
    monkeypatch.delenv("HALT_FILE_PATH")
    monkeypatch.delenv("AUDIT_LOG_PATH")

    # Act / Assert
    assert guard.halt_file_path() == guard.REPO_ROOT / "HALT"
    assert guard.audit_log_path() == guard.REPO_ROOT / "audit" / "audit.jsonl"
    assert (guard.REPO_ROOT / "triage_agent" / "guard.py").exists()


# --- fingerprints and the preview registry ---------------------------------


def test_fingerprint_is_key_order_independent_and_tool_specific():
    # Arrange
    a = {"ticket_id": "T-1", "amount_usd": 10}
    b = {"amount_usd": 10, "ticket_id": "T-1"}

    # Act
    fp_a = guard.payload_fingerprint("issue_credit", a)
    fp_b = guard.payload_fingerprint("issue_credit", b)
    fp_other_tool = guard.payload_fingerprint("draft_reply", a)

    # Assert
    assert fp_a == fp_b
    assert len(fp_a) == 64 and all(c in "0123456789abcdef" for c in fp_a)
    assert fp_a != fp_other_tool


def test_preview_performs_nothing_and_audits_preview():
    # Arrange
    perform = Recorder()

    # Act
    result = guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)

    # Assert
    assert result["requires_confirmation"] is True
    assert result["preview"] == CREDIT
    assert result["fingerprint"] == guard.payload_fingerprint("issue_credit", CREDIT)
    assert "No write performed" in result["message"]
    assert perform.calls == []
    assert _phases() == ["preview"]
    assert guard.read_audit()[0]["fingerprint"] == result["fingerprint"]


def test_confirm_without_preview_is_rejected_and_performs_nothing():
    # Arrange
    perform = Recorder()

    # Act
    result = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Assert
    assert result["error"] is True
    assert result["requires_confirmation"] is True
    assert "no matching preview" in result["detail"].lower()
    assert perform.calls == []
    assert _phases() == ["rejected_no_preview"]


def test_preview_then_confirm_performs_exactly_once_and_audits_confirm():
    # Arrange
    perform = Recorder({"credit_id": "CR-7", "status": "issued"})
    preview = guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)

    # Act
    result = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Assert
    assert result == {"credit_id": "CR-7", "status": "issued"}
    assert perform.calls == [CREDIT]
    assert _phases() == ["preview", "confirm"]
    confirm_record = guard.read_audit()[-1]
    assert confirm_record["fingerprint"] == preview["fingerprint"]
    assert "CR-7" in confirm_record["result_summary"]


def test_second_confirm_after_successful_write_is_rejected():
    # Arrange
    perform = Recorder()
    guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)
    guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Act
    again = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Assert
    assert again["error"] is True
    assert len(perform.calls) == 1
    assert _phases() == ["preview", "confirm", "rejected_no_preview"]


def test_expired_preview_is_rejected_after_ttl(monkeypatch, guard_env):
    # Arrange
    monkeypatch.setenv("PREVIEW_TTL_SECONDS", "60")
    perform = Recorder()
    guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)
    guard_env.clock.advance(61)

    # Act
    stale = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Assert
    assert stale["error"] is True
    assert perform.calls == []

    # A fresh preview confirmed inside the window still works
    guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)
    guard_env.clock.advance(59)
    fresh = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)
    assert fresh.get("error") is not True
    assert len(perform.calls) == 1


def test_confirm_with_payload_different_from_preview_is_rejected():
    # Arrange
    perform = Recorder()
    guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=perform)
    tweaked = {**CREDIT, "amount_usd": 26.0}

    # Act
    result = guard.guarded_write("issue_credit", tweaked, confirmed=True, perform=perform)

    # Assert
    assert result["error"] is True
    assert perform.calls == []
    assert _phases()[-1] == "rejected_no_preview"


# --- policy caps that survive approval -------------------------------------


def test_credit_over_cap_is_refused_even_when_previewed_and_confirmed(monkeypatch):
    # Arrange
    monkeypatch.setenv("MAX_CREDIT_USD", "100")
    perform = Recorder()
    big = {**CREDIT, "amount_usd": 100.01}
    guard.get_registry().register("issue_credit", big)  # simulate a prior preview

    # Act
    preview = guard.guarded_write("issue_credit", big, confirmed=False, perform=perform)
    confirm = guard.guarded_write("issue_credit", big, confirmed=True, perform=perform)

    # Assert
    assert preview["refused"] is True and preview["error"] is True
    assert confirm["refused"] is True and confirm["error"] is True
    assert "100" in confirm["detail"]
    assert perform.calls == []
    assert _phases() == ["refused_policy", "refused_policy"]


def test_credit_that_is_not_positive_is_refused():
    # Arrange
    perform = Recorder()

    # Act
    zero = guard.guarded_write("issue_credit", {**CREDIT, "amount_usd": 0}, False, perform)
    negative = guard.guarded_write("issue_credit", {**CREDIT, "amount_usd": -5}, False, perform)
    missing = guard.check_policy("issue_credit", {"ticket_id": "T-1"})

    # Assert
    assert zero["refused"] is True
    assert negative["refused"] is True
    assert missing is not None
    assert perform.calls == []


def test_reply_to_spam_ticket_is_refused():
    # Arrange
    perform = Recorder()

    # Act
    result = guard.guarded_write(
        "draft_reply", {**REPLY, "category": "spam"}, confirmed=False, perform=perform
    )

    # Assert
    assert result["refused"] is True
    assert "spam" in result["detail"]
    assert perform.calls == []
    assert _phases() == ["refused_policy"]


def test_reply_to_emergency_ticket_is_refused_whether_category_is_payload_or_context():
    # Arrange
    perform = Recorder()

    # Act
    in_payload = guard.guarded_write(
        "draft_reply", {**REPLY, "category": "emergency"}, confirmed=False, perform=perform
    )
    as_context = guard.guarded_write("draft_reply", REPLY, False, perform, category="emergency")

    # Assert
    for result in (in_payload, as_context):
        assert result["refused"] is True
        assert "escalated to a human" in result["detail"]
    assert perform.calls == []
    assert _phases() == ["refused_policy", "refused_policy"]


def test_redact_masks_card_like_numbers_in_nested_values_without_mutating_input():
    # Arrange
    original = {
        "tickets": [{"body": f"charge {VISA_TEST_NUMBER} please", "tags": ("billing",)}],
        "note": f"ref {NOT_A_CARD}",
        "total": 1,
    }

    # Act
    masked = guard.redact(original)

    # Assert
    assert masked["tickets"][0]["body"] == "charge **** please"
    assert masked["note"] == original["note"]  # Luhn-invalid run left alone
    assert masked["total"] == 1
    assert VISA_TEST_NUMBER in original["tickets"][0]["body"]  # input untouched
    assert "4111" not in json.dumps(masked)


def test_category_argument_feeds_policy_without_changing_the_preview():
    # Arrange: category comes from the ticket record, not from the model
    perform = Recorder()

    # Act
    refused = guard.guarded_write("draft_reply", REPLY, False, perform, category="spam")
    allowed = guard.guarded_write("draft_reply", REPLY, False, perform, category="billing")

    # Assert
    assert refused["refused"] is True
    assert allowed["requires_confirmation"] is True
    assert allowed["preview"] == REPLY  # category is context, not written
    assert perform.calls == []


def test_escalate_without_reason_is_refused():
    # Arrange
    perform = Recorder()

    # Act
    blank = guard.guarded_write(
        "escalate_ticket", {**ESCALATION, "reason": "   "}, False, perform
    )
    missing = guard.guarded_write("escalate_ticket", {"ticket_id": "T-1"}, False, perform)
    valid = guard.check_policy("escalate_ticket", ESCALATION)

    # Assert
    assert blank["refused"] is True
    assert missing["refused"] is True
    assert valid is None
    assert perform.calls == []


def test_card_like_number_is_refused_and_masked_in_audit():
    # Arrange
    perform = Recorder()
    payload = {**REPLY, "body": f"Please charge {VISA_TEST_NUMBER} again"}

    # Act
    result = guard.guarded_write("draft_reply", payload, confirmed=False, perform=perform)

    # Assert
    assert result["refused"] is True
    assert result["detail"] == "payload contains a card-like number"
    assert perform.calls == []
    raw_line = guard.audit_log_path().read_text(encoding="utf-8")
    assert "4111" not in raw_line
    assert guard.read_audit()[0]["payload"]["body"] == "Please charge **** again"


def test_luhn_invalid_digit_run_is_not_treated_as_a_card():
    # Arrange
    payload = {**REPLY, "body": f"Your order reference is {NOT_A_CARD}"}

    # Act
    reason = guard.check_policy("draft_reply", payload)
    record = guard.audit("draft_reply", "preview", payload, "test")

    # Assert
    assert reason is None
    assert record["payload"]["body"] == payload["body"]


# --- halted: nothing reaches the API ---------------------------------------


def test_halted_write_never_calls_perform(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_HALT", "1")
    perform = Recorder()
    guard.get_registry().register("issue_credit", CREDIT)

    # Act
    result = guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=perform)

    # Assert
    assert result["halted"] is True and result["error"] is True
    assert perform.calls == []
    assert _phases() == ["halted"]


def test_halted_read_never_calls_perform(guard_env):
    # Arrange
    guard_env.halt_file.write_text("")
    perform = Recorder({"total": 1, "tickets": [{"ticket_id": "T-1"}]})

    # Act
    result = guard.guarded_read("list_tickets", {"status": "open"}, perform)

    # Assert
    assert result["halted"] is True and result["error"] is True
    assert "halted" in result["detail"].lower()
    assert perform.calls == []
    assert _phases() == ["halted"]


# --- audit trail -----------------------------------------------------------


def test_audit_path_from_env_is_honored_and_lines_are_valid_json(monkeypatch, tmp_path):
    # Arrange: nested directory that does not exist yet
    target = tmp_path / "logs" / "nested" / "trail.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))

    # Act
    first = guard.audit("issue_credit", "preview", CREDIT, "preview issued", "abc")
    second = guard.audit("list_tickets", "read", {"status": "open"}, "total=3")

    # Assert
    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert all(set(r) == AUDIT_KEYS for r in records)
    assert records == [first, second]
    assert records[0]["ts"].endswith("+00:00")
    assert records[1]["fingerprint"] is None
    assert guard.read_audit(target) == records


def test_read_audits_with_count_summary_and_returns_result():
    # Arrange
    tickets = [{"ticket_id": "T-1"}, {"ticket_id": "T-2"}, {"ticket_id": "T-3"}]
    perform = Recorder({"total": 3, "tickets": tickets})

    # Act
    result = guard.guarded_read("list_tickets", {"status": "open"}, perform)

    # Assert
    assert result["total"] == 3
    assert perform.calls == [{"status": "open"}]
    record = guard.read_audit()[0]
    assert record["phase"] == "read"
    assert record["payload"] == {"status": "open"}
    assert "3" in record["result_summary"]


def test_confirm_audits_the_exception_when_perform_raises():
    # Arrange
    def boom(_payload: dict) -> dict:
        raise RuntimeError("socket closed mid-request")

    guard.guarded_write("issue_credit", CREDIT, confirmed=False, perform=boom)

    # Act / Assert
    with pytest.raises(RuntimeError):
        guard.guarded_write("issue_credit", CREDIT, confirmed=True, perform=boom)
    last = guard.read_audit()[-1]
    assert last["phase"] == "confirm"
    assert "RuntimeError" in last["result_summary"]
