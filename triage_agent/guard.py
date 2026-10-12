"""Safety guard that every tool call passes through.

Safety contract — five guarantees, enforced here in code rather than in the
prompt so they survive instruction edits and model bumps:

1. Read-only by default. A write tool called without ``confirmed=True``
   returns a preview of the exact payload and performs nothing.
2. Two-phase writes are enforced in code. A confirmed write runs only if the
   identical payload (same tool, same fields, same values) was previewed
   first, within the preview TTL, and each preview can be consumed exactly
   once. The model cannot skip the preview step by asserting approval.
3. The kill switch is checked on every call. ``AGENT_HALT=1`` (or a HALT file
   in the repo root) stops every read and write before it reaches the API.
   Nothing is cached, so flipping it takes effect on the next call.
4. Policy caps survive human approval. Limits such as ``MAX_CREDIT_USD``
   apply to every payload, including one the human has already approved;
   approval can never raise a cap.
5. Every decision is appended to a JSON-lines audit log before the result is
   returned: reads, previews, confirmations, refusals and halts, with
   card-like numbers masked.
"""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIT_LOG = Path("audit") / "audit.jsonl"
DEFAULT_HALT_FILE = Path("HALT")
DEFAULT_PREVIEW_TTL_SECONDS = 900.0
DEFAULT_MAX_CREDIT_USD = 150.0
HALT_VALUES = frozenset({"1", "true", "yes"})

CARD_MASK = "****"
CARD_REASON = "payload contains a card-like number"
HALTED_DETAIL = "Agent halted; no tool calls are being executed."
PREVIEW_MESSAGE = (
    "No write performed. Show this preview to the user and ask for explicit "
    "approval before calling again with confirmed=True."
)
REJECTED_DETAIL = (
    "Rejected: no matching preview for this payload. Call the tool without "
    "confirmed first, show the user the preview, and only confirm after "
    "explicit approval."
)

# 13-19 digits, optionally separated by single spaces or dashes, not embedded
# in a longer digit run. Candidates are then Luhn-checked.
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_SEPARATORS = re.compile(r"[ -]")


# -- configuration (read on every call so env changes apply immediately) ----


def _repo_path(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name)
    path = Path(raw) if raw else default
    return path if path.is_absolute() else REPO_ROOT / path


def _env_float(env_name: str, default: float) -> float:
    return float(os.environ.get(env_name) or default)


def halt_file_path() -> Path:
    return _repo_path("HALT_FILE_PATH", DEFAULT_HALT_FILE)


def audit_log_path() -> Path:
    return _repo_path("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG)


# -- kill switch ----------------------------------------------------------


def is_halted() -> bool:
    """True when AGENT_HALT is set (1/true/yes) or the HALT file exists.
    Evaluated fresh on every call; never cached."""
    flag = os.environ.get("AGENT_HALT", "").strip().lower()
    return flag in HALT_VALUES or halt_file_path().exists()


# -- previews -------------------------------------------------------------


def payload_fingerprint(tool: str, payload: dict) -> str:
    """sha256 of the tool name plus the canonical JSON form of the payload."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool}\n{canonical}".encode("utf-8")).hexdigest()


class PreviewRegistry:
    """Previewed payloads that may each be confirmed once before they expire.

    ``now`` is injectable so tests can advance time without sleeping."""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._expires_at: dict[str, float] = {}

    def register(self, tool: str, payload: dict) -> str:
        fingerprint = payload_fingerprint(tool, payload)
        ttl = _env_float("PREVIEW_TTL_SECONDS", DEFAULT_PREVIEW_TTL_SECONDS)
        self._expires_at = {**self._live(), fingerprint: self._now() + ttl}
        return fingerprint

    def consume(self, tool: str, payload: dict) -> bool:
        """True once per live preview; the preview is removed either way."""
        fingerprint = payload_fingerprint(tool, payload)
        live = self._live()
        self._expires_at = {k: v for k, v in live.items() if k != fingerprint}
        return fingerprint in live

    def reset(self) -> None:
        self._expires_at = {}

    def _live(self) -> dict[str, float]:
        now = self._now()
        return {k: v for k, v in self._expires_at.items() if v > now}


_registry: PreviewRegistry | None = None


def get_registry() -> PreviewRegistry:
    """Module-level singleton shared by all write tools. Tests replace it."""
    global _registry
    if _registry is None:
        _registry = PreviewRegistry()
    return _registry


def set_registry(registry: PreviewRegistry | None) -> None:
    global _registry
    _registry = registry


# -- card-like number detection and redaction ------------------------------


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def _is_card_like(candidate: str) -> bool:
    digits = _SEPARATORS.sub("", candidate)
    return 13 <= len(digits) <= 19 and _luhn_valid(digits)


def _mask_cards(text: str) -> str:
    def replace(match: re.Match) -> str:
        return CARD_MASK if _is_card_like(match.group(0)) else match.group(0)

    return _CARD_CANDIDATE.sub(replace, text)


def _redact(value):
    """Return a copy of ``value`` with card-like numbers in strings masked."""
    if isinstance(value, str):
        return _mask_cards(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _strings(value):
    """Yield every string nested anywhere inside ``value``."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _contains_card(payload) -> bool:
    return any(
        _is_card_like(match.group(0))
        for text in _strings(payload)
        for match in _CARD_CANDIDATE.finditer(text)
    )


# -- policy ---------------------------------------------------------------


def _credit_rule(payload: dict) -> str | None:
    amount = payload.get("amount_usd")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return "amount_usd must be a number"
    cap = _env_float("MAX_CREDIT_USD", DEFAULT_MAX_CREDIT_USD)
    if not 0 < amount <= cap:
        return (
            f"amount_usd must be greater than 0 and at most {cap:g} USD "
            f"(got {amount})"
        )
    return None


def _reply_rule(payload: dict) -> str | None:
    if payload.get("category") == "spam":
        return "replies are not sent to tickets classified as spam"
    return None


def _escalate_rule(payload: dict) -> str | None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "escalate_ticket requires a non-empty reason"
    return None


_POLICY_RULES: dict[str, Callable[[dict], str | None]] = {
    "issue_credit": _credit_rule,
    "draft_reply": _reply_rule,
    "escalate_ticket": _escalate_rule,
}


def check_policy(tool: str, payload: dict) -> str | None:
    """Return a refusal reason, or None when the payload is allowed.
    Applied before preview and again before confirm: approval never
    overrides policy."""
    if _contains_card(payload):
        return CARD_REASON
    rule = _POLICY_RULES.get(tool)
    return rule(payload) if rule else None


# -- audit ----------------------------------------------------------------


def audit(
    tool: str,
    phase: str,
    payload: dict,
    result_summary: str,
    fingerprint: str | None = None,
) -> dict:
    """Append one JSON line to the audit log (card numbers masked) and
    return the record. Phases: read, preview, confirm, rejected_no_preview,
    refused_policy, halted."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "phase": phase,
        "payload": _redact(payload),
        "fingerprint": fingerprint,
        "result_summary": result_summary,
    }
    path = audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return record


def read_audit(path: str | Path | None = None) -> list[dict]:
    target = Path(path) if path else audit_log_path()
    if not target.exists():
        return []
    with target.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _summarize(result) -> str:
    """Short, id- or count-style description of a tool result for the log."""
    if not isinstance(result, dict):
        return type(result).__name__
    if result.get("error"):
        return f"error: {str(result.get('detail', ''))[:120]}"
    ids = [f"{k}={v}" for k, v in result.items() if k == "id" or k.endswith("_id")]
    if ids:
        return ", ".join(ids)
    if "total" in result:
        return f"total={result['total']}"
    counts = [f"{k}={len(v)}" for k, v in result.items() if isinstance(v, list)]
    return ", ".join(counts) if counts else "ok"


# -- entry points ---------------------------------------------------------


def _halted_result(tool: str, payload: dict) -> dict:
    audit(tool, "halted", payload, HALTED_DETAIL)
    return {"halted": True, "error": True, "detail": HALTED_DETAIL}


def _perform_write(tool: str, payload: dict, perform, fingerprint: str) -> dict:
    try:
        result = perform(payload)
    except Exception as exc:
        summary = f"exception: {type(exc).__name__}: {exc}"
        audit(tool, "confirm", payload, summary, fingerprint)
        raise
    audit(tool, "confirm", payload, _summarize(result), fingerprint)
    return result


def guarded_write(
    tool: str,
    payload: dict,
    confirmed: bool,
    perform: Callable[[dict], dict],
    category: str | None = None,
) -> dict:
    """The single entry point for every write tool.

    ``category`` is context the tool looked up itself (e.g. the ticket's
    classification); it is merged into the policy check but is not part of
    the payload that gets previewed or written. ``perform`` is called at
    most once, and never on any refusal path."""
    if is_halted():
        return _halted_result(tool, payload)
    policy_payload = payload if category is None else {**payload, "category": category}
    reason = check_policy(tool, policy_payload)
    if reason:
        audit(tool, "refused_policy", payload, reason)
        return {"error": True, "refused": True, "detail": reason}
    registry = get_registry()
    if not confirmed:
        fingerprint = registry.register(tool, payload)
        audit(tool, "preview", payload, "preview issued; nothing written", fingerprint)
        return {
            "requires_confirmation": True,
            "preview": payload,
            "fingerprint": fingerprint,
            "message": PREVIEW_MESSAGE,
        }
    fingerprint = payload_fingerprint(tool, payload)
    if not registry.consume(tool, payload):
        audit(tool, "rejected_no_preview", payload, REJECTED_DETAIL, fingerprint)
        return {"error": True, "requires_confirmation": True, "detail": REJECTED_DETAIL}
    return _perform_write(tool, payload, perform, fingerprint)


def guarded_read(tool: str, params: dict, perform: Callable[[dict], dict]) -> dict:
    """Entry point for read tools: honors the kill switch and audits the
    call with a count-style summary."""
    if is_halted():
        return _halted_result(tool, params)
    result = perform(params)
    audit(tool, "read", params, _summarize(result))
    return result
