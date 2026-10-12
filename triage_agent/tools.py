"""Tools the triage agent can call.

Plain functions: ADK derives each tool's schema from the signature and its
description from the docstring, so the docstrings below are written for the
model (what the tool does, when to use it, what it returns, the approval
rule), not for a human reader.

Every read goes through guard.guarded_read (kill switch + audit) and masks
card-like numbers before the result reaches the model. Every write goes
through guard.guarded_write (kill switch + policy + two-phase approval +
audit). Nothing here reaches the API except through those two gates.
"""

import re
from datetime import date, datetime

from triage_agent import api_client, guard

CATEGORIES = frozenset(
    {"scheduling", "billing", "complaint", "warranty", "cancellation",
     "emergency", "spam", "sales", "other"}
)
ESCALATION_PRIORITIES = frozenset({"high", "emergency"})
DUPLICATE_WINDOW_HOURS = 24

# Life-safety signals. A ticket matching any of these is an emergency no
# matter how the model classified it; over-matching is the safe direction.
_EMERGENCY_PATTERNS = re.compile(
    r"\bgas\b.{0,20}\b(smell|leak|odou?r)|\b(smell|leak|odou?r)\b.{0,20}\bgas\b"
    r"|carbon monoxide|\bco\b.{0,15}\b(alarm|detector)"
    r"|\bsmok(e|ing)\b|\bfire\b|\bspark(s|ing|ed)?\b"
    r"|electrical.{0,20}burn|burning.{0,10}(smell|odou?r)|smells?.{0,20}burning",
    re.IGNORECASE | re.DOTALL,
)
_WHITESPACE = re.compile(r"\s+")


# -- helpers ----------------------------------------------------------------


def _invalid(detail: str) -> dict:
    """Structured argument error, returned before the guard is consulted."""
    return {"error": True, "invalid": True, "detail": detail}


def _client() -> api_client.HelpdeskClient:
    return api_client.get_client()


def _normalize(text) -> str:
    return _WHITESPACE.sub(" ", str(text or "").casefold()).strip()


def _within_hours(first, second, hours: float) -> bool:
    try:
        delta = datetime.fromisoformat(str(first)) - datetime.fromisoformat(str(second))
    except (TypeError, ValueError):  # unparseable, or naive vs aware
        return False
    return abs(delta.total_seconds()) <= hours * 3600


def _same_party(ticket: dict, other: dict) -> bool:
    contact = ticket.get("contact_id")
    email = _normalize(ticket.get("requester_email"))
    return bool(
        (contact and other.get("contact_id") == contact)
        or (email and _normalize(other.get("requester_email")) == email)
    )


def _duplicate_match(ticket: dict, other: dict) -> str | None:
    """Why ``other`` duplicates ``ticket``: "body", "subject+24h", or None."""
    if other.get("ticket_id") == ticket.get("ticket_id") or not _same_party(ticket, other):
        return None
    body = _normalize(ticket.get("body"))
    if body and _normalize(other.get("body")) == body:
        return "body"
    subject = _normalize(ticket.get("subject"))
    if subject and _normalize(other.get("subject")) == subject:
        if _within_hours(other.get("created_at"), ticket.get("created_at"), DUPLICATE_WINDOW_HOURS):
            return "subject+24h"
    return None


def _compact(ticket: dict, match: str) -> dict:
    keys = ("ticket_id", "status", "subject", "created_at")
    return {**{key: ticket.get(key) for key in keys}, "match": match}


def _emergency_override(ticket: dict, category: str) -> str:
    """Code-side override: an escalated ticket, or one whose text carries a
    life-safety signal, is an emergency whatever the model decided."""
    if ticket.get("status") == "escalated":
        return "emergency"
    text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"
    return "emergency" if _EMERGENCY_PATTERNS.search(text) else category


# -- read tools -------------------------------------------------------------


def list_open_tickets(status: str = "open", q: str = "") -> dict:
    """List helpdesk tickets. Call this first to see the queue, or with q to
    search subject and body (case-insensitive substring match).

    Args:
        status: Filter by status: "open" (default), "closed", "escalated",
            or "" for every status.
        q: Optional search text matched against subject and body.

    Returns:
        {"total": int, "tickets": [ticket, ...]}. Card-like numbers in ticket
        text arrive already masked as "****"; never try to recover them.
        On failure: {"error": True, "status_code": int | None, "detail": str}.
    """
    def perform(params: dict) -> dict:
        listing = _client().list_tickets(status=params["status"] or None, q=params["q"] or None)
        return guard.redact(listing)

    return guard.guarded_read("list_open_tickets", {"status": status, "q": q}, perform)


def get_ticket(ticket_id: str) -> dict:
    """Fetch one ticket in full: subject, body, status, priority, contact_id,
    requester_email, created_at, updated_at, tags. Call it before
    classifying or acting on a ticket.

    Args:
        ticket_id: Ticket id such as "T-2003".

    Returns:
        The ticket dict with card-like numbers masked as "****", or
        {"error": True, "status_code": 404, "detail": str} if it does not exist.
    """
    def perform(params: dict) -> dict:
        return guard.redact(_client().get_ticket(params["ticket_id"]))

    return guard.guarded_read("get_ticket", {"ticket_id": ticket_id}, perform)


def get_contact(contact_id: str = "", email: str = "") -> dict:
    """Look up a CRM contact by contact_id, or by email when the ticket has
    no contact_id (the email match is case-insensitive). Use it to confirm
    who the requester is, their service plan, open balance and notes.

    Args:
        contact_id: Contact id such as "C-1001". Takes precedence when given.
        email: Requester email; used only when contact_id is empty.

    Returns:
        The contact dict, or {"error": True, "status_code": 404, "detail": str}
        when nothing matches. A missing contact is a fact to report, never a
        reason to guess.
    """
    if not contact_id and not email:
        return _invalid("get_contact needs a contact_id or an email")

    def perform(params: dict) -> dict:
        if params["contact_id"]:
            return guard.redact(_client().get_contact(params["contact_id"]))
        found = _client().find_contacts(email=params["email"])
        if found.get("error"):
            return found
        if not found.get("contacts"):
            detail = f"no contact with email {params['email']}"
            return {"error": True, "status_code": 404, "detail": detail}
        return guard.redact(found["contacts"][0])

    params = {"contact_id": contact_id, "email": email}
    return guard.guarded_read("get_contact", params, perform)


def find_duplicates(ticket_id: str) -> dict:
    """Find other tickets (any status) from the same requester that duplicate
    this one: same contact_id or same email, and either an identical body
    (ignoring case and whitespace) or an identical subject created within
    24 hours. Deterministic, no model judgement. Call it before proposing a
    reply so a double-submit gets one answer, not two.

    Args:
        ticket_id: The ticket to check, such as "T-2025".

    Returns:
        {"ticket_id": str, "duplicates": [{"ticket_id", "status", "subject",
        "created_at", "match"}, ...], "total": int}, where "match" is "body"
        or "subject+24h". An error dict if the ticket cannot be fetched.
    """
    def perform(params: dict) -> dict:
        ticket = _client().get_ticket(params["ticket_id"])
        if ticket.get("error"):
            return ticket
        listing = _client().list_tickets()
        if listing.get("error"):
            return listing
        rows = [
            _compact(other, match)
            for other in listing.get("tickets", [])
            if (match := _duplicate_match(ticket, other))
        ]
        result = {"ticket_id": ticket["ticket_id"], "duplicates": rows, "total": len(rows)}
        return guard.redact(result)

    return guard.guarded_read("find_duplicates", {"ticket_id": ticket_id}, perform)


# -- write tools ------------------------------------------------------------


def draft_reply(ticket_id: str, text: str, category: str, confirmed: bool = False) -> dict:
    """Queue a reply to the customer on a ticket. Write it in the customer's
    language and never include card or bank numbers.

    Phase 1 (confirmed=False, default): returns a preview and writes nothing.
    Phase 2 (confirmed=True): performs the write only if the identical payload
    was previewed and the human explicitly approved it in this conversation.
    Never set confirmed=True on your own initiative.

    Args:
        ticket_id: Ticket id such as "T-2003".
        text: The full reply text.
        category: Your classification of the ticket, one of: scheduling,
            billing, complaint, warranty, cancellation, emergency, spam,
            sales, other. Replies to spam and to emergencies are refused.
            The tool treats a ticket as an emergency, whatever category you
            pass, when its status is "escalated" or its text reports a gas
            smell or leak, carbon monoxide, a CO alarm, smoke, fire, sparking
            or electrical burning.
        confirmed: See the two phases above.

    Returns:
        A preview dict, the created reply {"reply_id", "status": "queued", ...},
        or a structured refusal/error dict with "detail".
    """
    if category not in CATEGORIES:
        return _invalid(f"category must be one of {sorted(CATEGORIES)}")
    if not text.strip():
        return _invalid("text must not be blank")
    ticket = get_ticket(ticket_id)  # guarded: halts and 404s surface here
    if ticket.get("error"):
        return ticket
    effective = _emergency_override(ticket, category)
    payload = {"ticket_id": ticket_id, "text": text}

    def perform(p: dict) -> dict:
        return _client().create_reply(p["ticket_id"], p["text"])

    return guard.guarded_write("draft_reply", payload, confirmed, perform, category=effective)


def create_followup_task(
    ticket_id: str, assignee: str, due_date: str, note: str = "", confirmed: bool = False
) -> dict:
    """Create an internal follow-up task on a ticket for a person or queue
    (for example "dispatch", "billing", "manager"). Use it when the right
    action is human work rather than a customer reply: a callback, an
    invoice correction, a credit above the agent's cap, a dispatch change.

    Phase 1 (confirmed=False, default): returns a preview and writes nothing.
    Phase 2 (confirmed=True): performs the write only if the identical payload
    was previewed and the human explicitly approved it in this conversation.
    Never set confirmed=True on your own initiative.

    Args:
        ticket_id: Ticket id such as "T-2030".
        assignee: Person or queue that owns the task.
        due_date: ISO date, YYYY-MM-DD.
        note: What needs doing, in one or two sentences.
        confirmed: See the two phases above.

    Returns:
        A preview dict, the created task {"task_id", "status": "open", ...},
        or a structured error dict with "detail".
    """
    if not assignee.strip():
        return _invalid("assignee must not be blank")
    try:
        date.fromisoformat(due_date)
    except (TypeError, ValueError):
        return _invalid("due_date must be an ISO date (YYYY-MM-DD)")
    payload = {"ticket_id": ticket_id, "assignee": assignee, "due_date": due_date, "note": note}
    return guard.guarded_write(
        "create_followup_task", payload, confirmed, lambda p: _client().create_task(p)
    )


def issue_credit(
    ticket_id: str, contact_id: str, amount_usd: float, reason: str, confirmed: bool = False
) -> dict:
    """Issue an account credit to a contact against a ticket. The credit is
    created as "pending_review" for the billing team. Amounts above the
    policy cap (MAX_CREDIT_USD, 150 by default) are refused even with human
    approval; for those, propose create_followup_task for a manager instead.

    Phase 1 (confirmed=False, default): returns a preview and writes nothing.
    Phase 2 (confirmed=True): performs the write only if the identical payload
    was previewed and the human explicitly approved it in this conversation.
    Never set confirmed=True on your own initiative.

    Args:
        ticket_id: Ticket the credit relates to.
        contact_id: Contact receiving the credit; must exist in the CRM.
        amount_usd: Positive amount in USD.
        reason: One sentence a billing reviewer can act on.
        confirmed: See the two phases above.

    Returns:
        A preview dict, the created credit {"credit_id",
        "status": "pending_review", ...}, or a structured refusal/error dict
        with "detail".
    """
    try:
        amount = float(amount_usd)  # 89 and 89.0 must fingerprint identically
    except (TypeError, ValueError):
        return _invalid("amount_usd must be a number")
    payload = {
        "ticket_id": ticket_id, "contact_id": contact_id, "amount_usd": amount, "reason": reason
    }
    return guard.guarded_write(
        "issue_credit", payload, confirmed, lambda p: _client().create_credit(p)
    )


def escalate_ticket(
    ticket_id: str, reason: str, priority: str = "high", confirmed: bool = False
) -> dict:
    """Escalate a ticket to the on-call human lead: sets status "escalated"
    and records the reason. Use it FIRST, before any reply, for anything
    that could be a safety issue (gas smell or leak, carbon monoxide, CO
    alarm, smoke, fire, sparking, electrical burning) and for a customer
    left without service after a missed appointment window.

    Phase 1 (confirmed=False, default): returns a preview and writes nothing.
    Phase 2 (confirmed=True): performs the write only if the identical payload
    was previewed and the human explicitly approved it in this conversation.
    Never set confirmed=True on your own initiative.

    Args:
        ticket_id: Ticket id such as "T-2029".
        reason: Why it is being escalated, quoting the customer's own words.
        priority: "high" (default) or "emergency" (life-safety).
        confirmed: See the two phases above.

    Returns:
        A preview dict, the escalated ticket {"status": "escalated", ...},
        or a structured error dict with "detail".
    """
    if priority not in ESCALATION_PRIORITIES:
        return _invalid('priority must be "high" or "emergency"')
    payload = {"ticket_id": ticket_id, "reason": reason, "priority": priority}

    def perform(p: dict) -> dict:
        body = {"reason": p["reason"], "priority": p["priority"]}
        return _client().escalate_ticket(p["ticket_id"], body)

    return guard.guarded_write("escalate_ticket", payload, confirmed, perform)


ALL_TOOLS = [
    list_open_tickets, get_ticket, get_contact, find_duplicates,
    draft_reply, create_followup_task, issue_credit, escalate_ticket,
]
