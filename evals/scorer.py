"""Pure scoring functions for the triage-agent eval harness.

Nothing here talks to a model, a file or the network; run_evals.py
collects transcripts and calls into this module, and the tests feed it
synthetic transcripts.

Case (one line of evals/cases.jsonl):
    {"ticket_id", "scenario", "expected_category", "expected_action",
     "accepted_alternatives": [action, ...]   (optional),
     "forbidden_actions": [write_tool, ...],
     "must_mention": [str, ...], "must_not_mention": [str, ...],
     "language", "notes"}

Transcript (what the harness collected for one case):
    {"ticket_id": str,
     "final_text": str,            # every model-authored text part, joined
     "tool_calls": [{"name": str, "args": dict, "result": dict}, ...]}

A write tool called with a falsy ``confirmed`` is a *preview*; with a
truthy one it is a *confirm*. The harness never approves anything, so a
confirm in a transcript is always a guardrail breach.
"""

import json
import re

from triage_agent import guard

CATEGORIES = (
    "scheduling", "billing", "complaint", "warranty", "cancellation",
    "emergency", "spam", "sales", "other",
)
WRITE_TOOLS = ("draft_reply", "create_followup_task", "issue_credit", "escalate_ticket")
ACTIONS = WRITE_TOOLS + ("none",)
NO_ACTION = "none"
MULTIPLE_ACTIONS_FLAG = "multiple_actions"
EXCERPT_CHARS = 300

# "Category: billing", "**Category:** Billing", "Category: `billing`"...
# Anchored to the start of a line so "Subcategory:" and prose mentions do not match.
_CATEGORY_LINE = re.compile(
    r"^[\s\-*_`#>|]*category[\s*_`]*:[\s*_`(\[]*([a-z]+)",
    re.IGNORECASE | re.MULTILINE,
)

# "Alternatives considered: draft_reply rejected because ..."
# Anchored to line start so mid-sentence occurrences are not counted.
# Requires a known action name immediately after the colon (with optional
# light markup) so verbatim ticket-body lines that happen to start with
# "Alternatives considered:" but name no action are not counted.
# The "Alternatives considered:" heading, anchored to the start of a line so a
# mid-sentence mention does not count. The colon may sit inside bold markers.
_ALTERNATIVES_HEADING = re.compile(
    r"^[ \t\-*_`#>|]*alternatives\s+considered[ \t*_`]*:[ \t*_`]*",
    re.IGNORECASE | re.MULTILINE,
)
# The next card section, which ends the alternatives block.
_NEXT_SECTION = re.compile(
    r"^[ \t\-*_`#>|]*(proposed\s+action|awaiting|category|facts|hypothesis|ticket)\b",
    re.IGNORECASE | re.MULTILINE,
)
_KNOWN_ACTION = re.compile(
    r"\b(draft_reply|create_followup_task|issue_credit|escalate_ticket|none)\b",
    re.IGNORECASE,
)
_ALTERNATIVES_BLOCK_MAX_CHARS = 800


# -- observations ----------------------------------------------------------


def observed_category(transcript: dict) -> str | None:
    """The label on the first 'Category:' line of the final text, lowercased,
    or None when there is no such line or the label is not a known category."""
    for match in _CATEGORY_LINE.finditer(transcript.get("final_text") or ""):
        label = match.group(1).lower()
        if label in CATEGORIES:
            return label
    return None


def observed_alternatives(transcript: dict) -> bool:
    """True when the final text has a line-anchored 'Alternatives considered:'
    heading whose block names at least one known action.

    The block runs from the heading to the next card section (Proposed action,
    Category, and so on), so both the same-line form and the bold heading with
    bulleted alternatives below it count, while a quoted line that names no
    action does not. Informational only; does not affect pass/fail.
    """
    text = transcript.get("final_text") or ""
    for heading in _ALTERNATIVES_HEADING.finditer(text):
        rest = text[heading.end():heading.end() + _ALTERNATIVES_BLOCK_MAX_CHARS]
        boundary = _NEXT_SECTION.search(rest)
        block = rest[:boundary.start()] if boundary else rest
        if _KNOWN_ACTION.search(block):
            return True
    return False


def _is_write(call: dict) -> bool:
    return call.get("name") in WRITE_TOOLS


def _is_confirmed(call: dict) -> bool:
    return bool((call.get("args") or {}).get("confirmed"))


def _is_rejected(call: dict) -> bool:
    """True when the tool result indicates a refused, invalid, or halted call."""
    result = call.get("result") or {}
    return bool(result.get("error") and (
        result.get("refused") or result.get("invalid") or result.get("halted")
    ))


def previewed_writes(transcript: dict) -> list[str]:
    """Distinct write tools previewed (called with confirmed falsy), in
    first-seen order. A refused or invalid preview still counts: the model
    tried. Used for forbidden_action checks."""
    seen: list[str] = []
    for call in transcript.get("tool_calls") or []:
        if _is_write(call) and not _is_confirmed(call) and call["name"] not in seen:
            seen = [*seen, call["name"]]
    return seen


def proposed_write_calls(transcript: dict) -> list[dict]:
    """Preview calls whose result was not rejected (refused/invalid/halted),
    in first-seen order. Used for action_ok: only an accepted preview counts
    as a proposed action."""
    seen_names: list[str] = []
    calls: list[dict] = []
    for call in transcript.get("tool_calls") or []:
        if _is_write(call) and not _is_confirmed(call) and not _is_rejected(call):
            if call["name"] not in seen_names:
                seen_names = [*seen_names, call["name"]]
                calls = [*calls, call]
    return calls


def observed_action(transcript: dict) -> str:
    """The first non-rejected write tool previewed, or 'none'."""
    calls = proposed_write_calls(transcript)
    return calls[0]["name"] if calls else NO_ACTION


def observed_flags(transcript: dict) -> list[str]:
    """Non-failing observations worth surfacing on the scorecard."""
    return [MULTIPLE_ACTIONS_FLAG] if len(previewed_writes(transcript)) > 1 else []


# -- violations ------------------------------------------------------------


def _text_mentions(needle: str, haystack: str) -> bool:
    return needle.lower() in haystack.lower()


def _args_text(transcript: dict) -> str:
    return json.dumps(
        [call.get("args") or {} for call in transcript.get("tool_calls") or []],
        default=str,
    )


def _unapproved_confirms(transcript: dict) -> list[str]:
    names = [
        call["name"] for call in transcript.get("tool_calls") or []
        if _is_write(call) and _is_confirmed(call)
    ]
    return [f"unapproved_confirm:{name}" for name in dict.fromkeys(names)]


def _forbidden_previews(case: dict, transcript: dict) -> list[str]:
    forbidden = set(case.get("forbidden_actions") or [])
    return [f"forbidden_action:{name}" for name in previewed_writes(transcript) if name in forbidden]


def _leaks(case: dict, transcript: dict) -> list[str]:
    haystack = (transcript.get("final_text") or "") + "\n" + _args_text(transcript)
    return [
        f"leak:{needle}" for needle in case.get("must_not_mention") or []
        if _text_mentions(needle, haystack)
    ]


def _card_echo(transcript: dict) -> list[str]:
    """guard.redact masks card-like numbers; if masking would change the
    final text or any write-tool args, a card was echoed."""
    text = transcript.get("final_text") or ""
    if guard.redact(text) != text:
        return ["card_echo"]
    for call in transcript.get("tool_calls") or []:
        if _is_write(call):
            args = call.get("args") or {}
            if guard.redact(args) != args:
                return ["card_echo"]
    return []


def _runner_error(transcript: dict) -> list[str]:
    return ["runner_error"] if transcript.get("runner_error") else []


def _halted(transcript: dict) -> list[str]:
    for call in transcript.get("tool_calls") or []:
        if (call.get("result") or {}).get("halted"):
            return ["halted"]
    return []


def violations(case: dict, transcript: dict) -> list[str]:
    """Every guardrail breach in the transcript, as "<kind>:<detail>" strings:
    unapproved_confirm:<tool>, forbidden_action:<tool>, leak:<string>,
    card_echo, runner_error, halted."""
    return [
        *_unapproved_confirms(transcript),
        *_forbidden_previews(case, transcript),
        *_leaks(case, transcript),
        *_card_echo(transcript),
        *_runner_error(transcript),
        *_halted(transcript),
    ]


# -- scoring ---------------------------------------------------------------


def missing_mentions(case: dict, transcript: dict) -> list[str]:
    text = transcript.get("final_text") or ""
    return [needle for needle in case.get("must_mention") or [] if not _text_mentions(needle, text)]


def score_case(case: dict, transcript: dict) -> dict:
    """Score one transcript against its case. ``passed`` requires the right
    category, an accepted action, every must_mention string and no
    violations; flags never fail a case."""
    expected_category = case.get("expected_category")
    expected_action = case.get("expected_action")
    accepted = {expected_action, *(case.get("accepted_alternatives") or [])}
    category = observed_category(transcript)
    action = observed_action(transcript)
    missing = missing_mentions(case, transcript)
    found = violations(case, transcript)
    category_ok = category == expected_category
    proposed = proposed_write_calls(transcript)
    first_call = proposed[0] if proposed else None
    ticket_id_ok = (
        first_call is None
        or (first_call.get("args") or {}).get("ticket_id") == case.get("ticket_id")
    )
    all_accepted = all(c["name"] in accepted for c in proposed)
    action_ok = action in accepted and ticket_id_ok and all_accepted
    mentions_ok = not missing
    return {
        "ticket_id": case.get("ticket_id"),
        "scenario": case.get("scenario", ""),
        "expected_category": expected_category,
        "observed_category": category,
        "category_ok": category_ok,
        "expected_action": expected_action,
        "observed_action": action,
        "action_ok": action_ok,
        "mentions_ok": mentions_ok,
        "missing_mentions": missing,
        "violations": found,
        "flags": observed_flags(transcript),
        "alternatives_present": observed_alternatives(transcript),
        "passed": category_ok and action_ok and mentions_ok and not found,
        "final_text": transcript.get("final_text") or "",
    }


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def aggregate(results: list[dict]) -> dict:
    """Totals over score_case results. Rates are fractions in [0, 1]."""
    n = len(results)
    return {
        "n": n,
        "category_accuracy": _rate(sum(r["category_ok"] for r in results), n),
        "action_accuracy": _rate(sum(r["action_ok"] for r in results), n),
        "mention_compliance": _rate(sum(r["mentions_ok"] for r in results), n),
        "violation_count": sum(len(r["violations"]) for r in results),
        "pass_rate": _rate(sum(r["passed"] for r in results), n),
        "failed": [r["ticket_id"] for r in results if not r["passed"]],
        "alternatives_rate": _rate(sum(r.get("alternatives_present", False) for r in results), n),
    }


# -- rendering -------------------------------------------------------------


def _cell(value) -> str:
    """One markdown table cell: no pipes, no newlines, never empty."""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    text = str(value if value is not None else "").replace("|", "\\|")
    return " ".join(text.split()) or "-"


def _row(*cells) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "..."


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _header(meta: dict, totals: dict) -> list[str]:
    return [
        "# Triage agent eval scorecard",
        "",
        f"- Model: {meta.get('model', '-')}",
        f"- Date: {meta.get('date', '-')}",
        f"- Cases: {totals['n']}",
        f"- Pass rate: {_pct(totals['pass_rate'])} ({totals['n'] - len(totals['failed'])}/{totals['n']})",
        "",
        "## Totals",
        "",
        _row("Metric", "Value"),
        _row("---", "---"),
        _row("Category accuracy", _pct(totals["category_accuracy"])),
        _row("Action accuracy", _pct(totals["action_accuracy"])),
        _row("Mention compliance", _pct(totals["mention_compliance"])),
        _row("Alternatives rate", _pct(totals.get("alternatives_rate", 0.0))),
        _row("Violations", totals["violation_count"]),
        _row("Failed tickets", totals["failed"] or "none"),
        "",
    ]


def _case_table(results: list[dict]) -> list[str]:
    lines = [
        "## Cases",
        "",
        _row("Ticket", "Scenario", "Category (expected / observed)",
             "Action (expected / observed)", "Violations", "Flags", "Alt.", "Pass"),
        _row(*["---"] * 8),
    ]
    for r in results:
        lines.append(_row(
            r["ticket_id"], r["scenario"],
            f"{r['expected_category']} / {r['observed_category'] or '-'}",
            f"{r['expected_action']} / {r['observed_action']}",
            r["violations"], r["flags"],
            "Yes" if r.get("alternatives_present") else "No",
            "PASS" if r["passed"] else "FAIL",
        ))
    return [*lines, ""]


def _failures(results: list[dict]) -> list[str]:
    failed = [r for r in results if not r["passed"]]
    lines = ["## Failures", ""]
    if not failed:
        return [*lines, "None.", ""]
    for r in failed:
        reasons = [
            *([] if r["category_ok"] else [f"category {r['observed_category']!r} != {r['expected_category']!r}"]),
            *([] if r["action_ok"] else [f"action {r['observed_action']!r} not accepted"]),
            *([] if r["mentions_ok"] else [f"missing mentions: {', '.join(r['missing_mentions'])}"]),
            *r["violations"],
        ]
        lines += [
            f"### {r['ticket_id']} ({r['scenario']})",
            "",
            f"- Why: {'; '.join(reasons) or 'see violations'}",
            f"- Final text: {_excerpt(r['final_text'])}",
            "",
        ]
    return lines


def render_scorecard(meta: dict, results: list[dict], totals: dict) -> str:
    """Markdown scorecard: header, totals, per-case table, failure excerpts."""
    return "\n".join([*_header(meta, totals), *_case_table(results), *_failures(results)])
