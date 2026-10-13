"""Tests for evals/harvest_feedback.py.

Covers: happy paths (rejected and edited), deduplication, append-without-
duplicates, rejected-vs-edited case mapping, card masking, ignoring non-
feedback audit lines, and the CLI entry point.
"""

import json
from pathlib import Path

import pytest

from evals import harvest_feedback


# -- helpers ----------------------------------------------------------------


def _audit_line(
    phase: str = "human_rejected",
    ticket_id: str = "T-2006",
    decision: str = "rejected",
    reason: str = "wrong action",
    proposed_tool: str = "draft_reply",
    edited_text: str = "",
    ts: str = "2026-10-13T12:00:00+00:00",
) -> str:
    record = {
        "ts": ts,
        "tool": "record_human_decision",
        "phase": phase,
        "payload": {
            "ticket_id": ticket_id,
            "decision": decision,
            "reason": reason,
            "proposed_tool": proposed_tool,
            "edited_text": edited_text,
        },
        "fingerprint": None,
        "result_summary": "recorded",
    }
    return json.dumps(record)


def _non_feedback_line(ticket_id: str = "T-2006") -> str:
    return json.dumps({
        "ts": "2026-10-13T12:00:00+00:00",
        "tool": "get_ticket",
        "phase": "read",
        "payload": {"ticket_id": ticket_id},
        "fingerprint": None,
        "result_summary": f"ticket_id={ticket_id}",
    })


def _write_audit(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_cases(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# -- happy paths ------------------------------------------------------------


def test_harvest_rejected_creates_case_with_forbidden_action(tmp_path):
    # Arrange
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [
        _audit_line(
            phase="human_rejected", ticket_id="T-2006", decision="rejected",
            reason="not appropriate for a billing dispute", proposed_tool="draft_reply",
        ),
    ])

    # Act
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert
    assert new_count == 1 and total == 1
    cases = _read_cases(out_path)
    assert len(cases) == 1
    c = cases[0]
    assert c["ticket_id"] == "T-2006"
    assert c["scenario"] == "feedback-rejected-T-2006"
    assert c["expected_category"] == ""
    assert c["expected_action"] == ""
    assert "draft_reply" in c["forbidden_actions"]
    assert c["source"] == "human_feedback"
    assert c["needs_labeling"] is True
    assert c["reason"] == "not appropriate for a billing dispute"
    assert c["recorded_at"] == "2026-10-13T12:00:00+00:00"


def test_harvest_edited_creates_case_with_expected_action_and_must_mention(tmp_path):
    # Arrange
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [
        _audit_line(
            phase="human_edited", ticket_id="T-2007", decision="edited",
            reason="credit is too small", proposed_tool="issue_credit",
            edited_text="please issue credit of fifty dollars for the diagnostic",
        ),
    ])

    # Act
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert
    assert new_count == 1 and total == 1
    c = _read_cases(out_path)[0]
    assert c["ticket_id"] == "T-2007"
    assert c["scenario"] == "feedback-edited-T-2007"
    assert c["expected_action"] == "issue_credit"
    assert len(c["must_mention"]) <= 3
    assert all(len(w) >= 5 for w in c["must_mention"])
    assert c["source"] == "human_feedback"
    assert c["needs_labeling"] is True
    assert c["reason"] == "credit is too small"


def test_harvest_edited_with_no_long_words_leaves_must_mention_empty(tmp_path):
    # Arrange: short words only in edited_text
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [
        _audit_line(
            phase="human_edited", ticket_id="T-2007", decision="edited",
            reason="yes", proposed_tool="issue_credit",
            edited_text="yes no up do it",
        ),
    ])

    # Act
    harvest_feedback.harvest(audit_path, out_path)

    # Assert
    c = _read_cases(out_path)[0]
    assert c["must_mention"] == []


# -- dedup and append -------------------------------------------------------


def test_harvest_deduplicates_same_ticket_decision_reason_within_one_run(tmp_path):
    # Arrange: same audit line twice
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    line = _audit_line(phase="human_rejected", reason="too aggressive")
    _write_audit(audit_path, [line, line])

    # Act
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert: deduplicated to one case
    assert new_count == 1 and total == 1


def test_harvest_appends_without_duplicating_existing_cases(tmp_path):
    # Arrange: first harvest writes one case
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [_audit_line(phase="human_rejected", reason="wrong tool")])
    harvest_feedback.harvest(audit_path, out_path)

    # Act: second harvest from same audit
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert: nothing new added, total still 1
    assert new_count == 0 and total == 1
    assert len(_read_cases(out_path)) == 1


def test_harvest_new_line_appended_without_duplicating_old_line(tmp_path):
    # Arrange: first harvest with one line; second audit has that plus a new one
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    line_a = _audit_line(reason="wrong tool", ticket_id="T-2006")
    line_b = _audit_line(reason="too terse", ticket_id="T-2007")

    _write_audit(audit_path, [line_a])
    harvest_feedback.harvest(audit_path, out_path)

    _write_audit(audit_path, [line_a, line_b])
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    assert new_count == 1 and total == 2
    ids = [c["ticket_id"] for c in _read_cases(out_path)]
    assert "T-2006" in ids and "T-2007" in ids


# -- rejected vs edited mapping --------------------------------------------


def test_harvest_rejected_vs_edited_mapping(tmp_path):
    # Arrange: one rejected, one edited
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [
        _audit_line(
            phase="human_rejected", ticket_id="T-2006", decision="rejected",
            reason="wrong", proposed_tool="draft_reply",
        ),
        _audit_line(
            phase="human_edited", ticket_id="T-2007", decision="edited",
            reason="amount", proposed_tool="issue_credit",
            edited_text="please change the credit amount to seventy five",
        ),
    ])

    # Act
    harvest_feedback.harvest(audit_path, out_path)

    # Assert
    cases = {c["ticket_id"]: c for c in _read_cases(out_path)}
    assert cases["T-2006"]["expected_action"] == ""
    assert cases["T-2006"]["forbidden_actions"] == ["draft_reply"]
    assert cases["T-2006"]["must_mention"] == []
    assert cases["T-2007"]["expected_action"] == "issue_credit"
    assert cases["T-2007"]["forbidden_actions"] == []


# -- card masking ----------------------------------------------------------


def test_harvest_card_numbers_in_reason_stay_masked(tmp_path):
    # The audit already masks card numbers; harvest must not un-mask them
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    # Simulate an already-masked audit line (guard.audit redacts before writing)
    _write_audit(audit_path, [
        _audit_line(phase="human_rejected", reason="card **** was already masked by the guard"),
    ])

    # Act
    harvest_feedback.harvest(audit_path, out_path)

    # Assert: the literal PAN never appears
    content = out_path.read_text(encoding="utf-8")
    assert "4111111111111111" not in content


# -- ignoring non-feedback lines -------------------------------------------


def test_harvest_ignores_non_feedback_audit_lines(tmp_path):
    # Arrange: only read/preview/confirm lines
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [_non_feedback_line()])

    # Act
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert
    assert new_count == 0 and total == 0
    assert not out_path.exists()


# -- CLI entry point -------------------------------------------------------


def test_harvest_main_with_explicit_audit_writes_cases_and_prints_summary(tmp_path, capsys):
    # Arrange
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [_audit_line(reason="wrong tool")])

    # Act
    code = harvest_feedback.main(["--audit", str(audit_path), "--out", str(out_path)])

    # Assert
    assert code == 0
    out = capsys.readouterr().out
    assert "1 new cases" in out and str(out_path) in out


def test_harvest_main_no_audit_file_found_returns_1(tmp_path, monkeypatch, capsys):
    # Arrange: no audit file at the default locations
    monkeypatch.setattr(harvest_feedback, "AUDIT_DEFAULTS", [])

    # Act
    code = harvest_feedback.main(["--out", str(tmp_path / "feedback.jsonl")])

    # Assert
    assert code == 1
    assert "no audit file" in capsys.readouterr().out.lower()


# -- regression: Finding F-03 (defense-in-depth re-masking) ----------------


def test_harvest_remasks_unmasked_pan_in_reason(tmp_path):
    """Regression F-03: _make_case must apply guard.redact to reason and
    edited_text before storing them, even when the audit file was written
    without masking (defense-in-depth against external writers)."""
    pan = "4111111111111111"
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    # Write a line with an unmasked PAN directly (bypassing guard.audit)
    record = {
        "ts": "2026-10-13T12:00:00+00:00",
        "tool": "record_human_decision",
        "phase": "human_rejected",
        "payload": {
            "ticket_id": "T-2006",
            "decision": "rejected",
            "reason": f"card {pan} was flagged",
            "proposed_tool": "draft_reply",
            "edited_text": "",
        },
        "fingerprint": None,
        "result_summary": "recorded",
    }
    audit_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    harvest_feedback.harvest(audit_path, out_path)

    content = out_path.read_text(encoding="utf-8")
    assert pan not in content, "unmasked PAN must not appear in harvested cases"
    assert "****" in content


def test_harvest_remasks_unmasked_pan_in_edited_text(tmp_path):
    """Regression F-03: guard.redact must also be applied to edited_text."""
    pan = "4111111111111111"
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    record = {
        "ts": "2026-10-13T12:00:00+00:00",
        "tool": "record_human_decision",
        "phase": "human_edited",
        "payload": {
            "ticket_id": "T-2007",
            "decision": "edited",
            "reason": "changed amount",
            "proposed_tool": "issue_credit",
            "edited_text": f"please charge card {pan} for the balance",
        },
        "fingerprint": None,
        "result_summary": "recorded",
    }
    audit_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    harvest_feedback.harvest(audit_path, out_path)

    content = out_path.read_text(encoding="utf-8")
    assert pan not in content


# -- regression: Finding F-04 (malformed JSON lines) -----------------------


def test_harvest_skips_malformed_json_lines_in_audit(tmp_path):
    """Regression F-04: _read_jsonl must skip malformed JSON lines in the
    audit file without crashing, and still process the valid lines."""
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    good_line = _audit_line(phase="human_rejected", reason="wrong tool")
    bad_line = "NOT VALID JSON {{{{"
    audit_path.write_text(good_line + "\n" + bad_line + "\n", encoding="utf-8")

    # Act: must not raise
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    # Assert: good line processed, bad line skipped
    assert new_count == 1 and total == 1


def test_harvest_skips_malformed_json_in_existing_output_file(tmp_path):
    """Regression F-04: _read_jsonl called on the output file for dedup must
    also handle malformed lines without crashing."""
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    _write_audit(audit_path, [_audit_line(phase="human_rejected", reason="too aggressive")])
    # Corrupt the output file
    out_path.write_text("NOT VALID JSON\n", encoding="utf-8")

    # Act: must not raise; corrupt output lines are skipped for dedup
    new_count, total = harvest_feedback.harvest(audit_path, out_path)

    assert new_count >= 1


def test_harvest_warns_on_stderr_for_malformed_audit_line(tmp_path, capsys):
    """A skipped malformed line is reported on stderr with its file and line
    number, so a damaged audit file is never silently shrunk."""
    audit_path = tmp_path / "audit.jsonl"
    out_path = tmp_path / "feedback.jsonl"
    good_line = _audit_line(phase="human_rejected", reason="wrong tool")
    bad_line = "NOT VALID JSON {{{{"
    audit_path.write_text(good_line + "\n" + bad_line + "\n", encoding="utf-8")

    harvest_feedback.harvest(audit_path, out_path)

    err = capsys.readouterr().err
    assert "skipped malformed JSON" in err and "line 2" in err
