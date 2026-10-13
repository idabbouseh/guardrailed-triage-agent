"""Harvest human-feedback audit lines into candidate eval cases.

    python -m evals.harvest_feedback
           [--audit evals/audit-latest.jsonl]
           [--out evals/feedback_cases.jsonl]

Reads lines with phase "human_rejected" or "human_edited" from the audit
log, converts them to case-file rows (needs_labeling=True, no expected
category), de-duplicates by (ticket_id, scenario, reason), and appends to
the output file without repeating rows already there.

Feedback cases are NOT added to evals/cases.jsonl automatically. They form
a separate file that a human labels before they join the eval as a held-out
set. Card numbers in the reason field are already masked by guard.audit before
they reach this script, so the output inherits that masking automatically.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from triage_agent import guard

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "evals"
AUDIT_DEFAULTS: list[Path] = [
    EVALS_DIR / "audit-latest.jsonl",
    REPO_ROOT / "audit" / "audit.jsonl",
]
DEFAULT_OUT = EVALS_DIR / "feedback_cases.jsonl"

_FEEDBACK_PHASES = frozenset({"human_rejected", "human_edited"})
# Only alphabetic words; good enough for English and avoids numeric tokens.
_WORD = re.compile(r"[A-Za-z]+")
_MIN_WORD_LEN = 5
_MAX_MUST_MENTION = 3


def _distinctive_words(text: str) -> list[str]:
    """Up to _MAX_MUST_MENTION distinct words of at least _MIN_WORD_LEN chars."""
    seen: list[str] = []
    for word in _WORD.findall(text or ""):
        if len(word) >= _MIN_WORD_LEN and word.lower() not in [w.lower() for w in seen]:
            seen = [*seen, word]
        if len(seen) >= _MAX_MUST_MENTION:
            break
    return seen


def _make_case(record: dict) -> dict | None:
    """Build one feedback case from an audit record, or return None if invalid.

    Card-like numbers in reason and edited_text are re-masked here as a
    defense-in-depth measure: guard.audit normally masks them before writing,
    but an externally-written audit file might not.
    """
    payload = record.get("payload") or {}
    ticket_id = payload.get("ticket_id")
    decision = payload.get("decision")
    reason = guard.redact(payload.get("reason", ""))
    proposed_tool = payload.get("proposed_tool", "")
    edited_text = guard.redact(payload.get("edited_text", ""))
    ts = record.get("ts", "")

    if not ticket_id or decision not in {"rejected", "edited"}:
        return None

    scenario = f"feedback-{decision}-{ticket_id}"

    if decision == "rejected":
        forbidden = [proposed_tool] if proposed_tool else []
        return {
            "ticket_id": ticket_id,
            "scenario": scenario,
            "expected_category": "",
            "expected_action": "",
            "accepted_alternatives": [],
            "forbidden_actions": forbidden,
            "must_mention": [],
            "must_not_mention": [],
            "language": "en",
            "notes": "",
            "source": "human_feedback",
            "needs_labeling": True,
            "reason": reason,
            "recorded_at": ts,
        }

    # decision == "edited"
    words = _distinctive_words(edited_text) if edited_text else []
    return {
        "ticket_id": ticket_id,
        "scenario": scenario,
        "expected_category": "",
        "expected_action": proposed_tool,
        "accepted_alternatives": [],
        "forbidden_actions": [],
        "must_mention": words,
        "must_not_mention": [],
        "language": "en",
        "notes": "",
        "source": "human_feedback",
        "needs_labeling": True,
        "reason": reason,
        "recorded_at": ts,
    }


def _dedup_key(case: dict) -> tuple:
    """Stable identity for a case: ticket, scenario (encodes decision), reason."""
    return (case.get("ticket_id", ""), case.get("scenario", ""), case.get("reason", ""))


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skipping blank and malformed lines without crashing.

    Malformed lines (for example a partial write at the end of an audit file)
    are skipped with a warning on stderr naming the file and line number, so a
    damaged file is visible rather than silently shrinking the harvest.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"warning: skipped malformed JSON in {path} line {line_no}: {exc.msg}", file=sys.stderr)
    return rows


def harvest(audit_path: Path, out_path: Path) -> tuple[int, int]:
    """Read audit_path, extract feedback lines, append new cases to out_path.

    Returns (new_cases_written, total_cases_in_file).
    """
    records = _read_jsonl(audit_path)
    candidates = [
        case
        for record in records
        if record.get("phase") in _FEEDBACK_PHASES
        if (case := _make_case(record)) is not None
    ]

    existing = _read_jsonl(out_path)
    existing_keys = {_dedup_key(c) for c in existing}

    new_cases: list[dict] = []
    seen_in_batch: set = set()
    for case in candidates:
        key = _dedup_key(case)
        if key not in existing_keys and key not in seen_in_batch:
            new_cases = [*new_cases, case]
            seen_in_batch = {*seen_in_batch, key}

    if new_cases:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as handle:
            for case in new_cases:
                handle.write(json.dumps(case) + "\n")

    return len(new_cases), len(existing) + len(new_cases)


def find_audit(path: str | None) -> Path | None:
    """Return the audit path to use: explicit arg, or first existing default."""
    if path:
        return Path(path)
    for candidate in AUDIT_DEFAULTS:
        if candidate.exists():
            return candidate
    return None


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harvest_feedback", description=__doc__
    )
    parser.add_argument(
        "--audit",
        help=(
            "audit JSONL path "
            "(defaults to evals/audit-latest.jsonl or audit/audit.jsonl)"
        ),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    audit_path = find_audit(args.audit)
    if audit_path is None:
        print("no audit file found; run the agent first or pass --audit")
        return 1
    new_count, total = harvest(audit_path, args.out)
    print(
        f"harvest_feedback: {new_count} new cases added, {total} total in {args.out}"
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
