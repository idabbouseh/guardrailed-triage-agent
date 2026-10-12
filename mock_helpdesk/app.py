"""Mock helpdesk + CRM API for a small home-services business.

Shaped like the SaaS APIs a small business actually runs on:
OAuth 2.0 client_credentials at POST /oauth2/token, Bearer token on every
call, 404s on unknown ids, 201s on creates, and no delete endpoints at all
(the agent cannot destroy what the API cannot destroy).

Routes (all under /api/v1, all require a Bearer token):
  GET  /health
  GET  /tickets?status=&contact_id=&q=      GET  /tickets/{ticket_id}
  GET  /contacts?email=                     GET  /contacts/{contact_id}
  POST /tickets/{ticket_id}/replies         POST /tickets/{ticket_id}/escalate
  POST /tasks                               POST /credits

Writes only ever append to the in-memory stores or replace a ticket dict
with a new one; nothing is mutated in place and nothing is deleted.

Run:  uvicorn mock_helpdesk.app:app --port 8086
"""

import os
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Form, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from . import data

CLIENT_ID = os.environ.get("HELPDESK_CLIENT_ID", "demo-client")
CLIENT_SECRET = os.environ.get("HELPDESK_CLIENT_SECRET", "demo-secret")
TOKEN_TTL_SECONDS = 3600
# Server-side backstop on a single credit. The agent's own cap (MAX_CREDIT_USD)
# is lower; this exists so a bug in the agent cannot exceed what a human
# dispatcher is allowed to approve in the real tool either.
MAX_CREDIT_BACKSTOP_USD = 500.0

app = FastAPI(title="Mock Helpdesk + CRM API (Northline Home Services)", version="0.1")

# access_token -> expiry epoch seconds
_tokens: dict[str, float] = {}


@app.post("/oauth2/token")
def issue_token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    if grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(status_code=401, detail="invalid_client")
    token = secrets.token_urlsafe(24)
    _tokens[token] = time.time() + TOKEN_TTL_SECONDS
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
    }


def require_auth(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    expiry = _tokens.get(token)
    if expiry is None or expiry < time.time():
        raise HTTPException(status_code=401, detail="invalid or expired token")


# -- request models ----------------------------------------------------------
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ReplyIn(BaseModel):
    text: NonBlankStr


class TaskIn(BaseModel):
    ticket_id: NonBlankStr
    assignee: NonBlankStr
    due_date: NonBlankStr  # ISO date; the mock stores it, it does not schedule
    note: str = ""


class CreditIn(BaseModel):
    contact_id: NonBlankStr
    ticket_id: NonBlankStr
    amount_usd: float = Field(gt=0, le=MAX_CREDIT_BACKSTOP_USD)
    reason: NonBlankStr


class EscalateIn(BaseModel):
    reason: NonBlankStr
    priority: Literal["high", "emergency"]


# -- helpers -----------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_id(prefix: str, rows: list[dict]) -> str:
    return f"{prefix}-{len(rows) + 1:04d}"


def _ticket_index_or_404(ticket_id: str) -> int:
    for idx, ticket in enumerate(data.TICKETS):
        if ticket["ticket_id"] == ticket_id:
            return idx
    raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")


def _ticket_or_404(ticket_id: str) -> dict:
    return data.TICKETS[_ticket_index_or_404(ticket_id)]


def _contact_or_404(contact_id: str) -> dict:
    for contact in data.CONTACTS:
        if contact["contact_id"] == contact_id:
            return contact
    raise HTTPException(status_code=404, detail=f"contact {contact_id} not found")


def _ticket_matches(
    ticket: dict, status: str | None, contact_id: str | None, q: str | None
) -> bool:
    if status and ticket["status"] != status:
        return False
    if contact_id and ticket["contact_id"] != contact_id:
        return False
    if q:
        # casefold (not lower) so accented names like Åström match either case.
        haystack = f"{ticket['subject']}\n{ticket['body']}".casefold()
        return q.casefold() in haystack
    return True


# -- routes ------------------------------------------------------------------
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])


@api.get("/health")
def health():
    return {
        "status": "ok",
        "tickets": len(data.TICKETS),
        "contacts": len(data.CONTACTS),
    }


@api.get("/tickets")
def list_tickets(status: str | None = None, contact_id: str | None = None, q: str | None = None):
    tickets = [t for t in data.TICKETS if _ticket_matches(t, status, contact_id, q)]
    return {"total": len(tickets), "tickets": tickets}


@api.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    return _ticket_or_404(ticket_id)


@api.get("/contacts")
def list_contacts(email: str | None = None):
    contacts = data.CONTACTS
    if email:
        wanted = email.strip().casefold()
        contacts = [c for c in contacts if c["email"].casefold() == wanted]
    return {"total": len(contacts), "contacts": contacts}


@api.get("/contacts/{contact_id}")
def get_contact(contact_id: str):
    return _contact_or_404(contact_id)


@api.post("/tickets/{ticket_id}/replies", status_code=201)
def create_reply(ticket_id: str, body: ReplyIn):
    _ticket_or_404(ticket_id)
    reply = {
        "reply_id": _next_id("R", data.REPLIES),
        "ticket_id": ticket_id,
        "status": "queued",
        "text": body.text,
        "created_at": _now_iso(),
    }
    data.REPLIES.append(reply)
    return reply


@api.post("/tasks", status_code=201)
def create_task(body: TaskIn):
    _ticket_or_404(body.ticket_id)
    task = {
        "task_id": _next_id("K", data.TASKS),
        "ticket_id": body.ticket_id,
        "assignee": body.assignee,
        "due_date": body.due_date,
        "note": body.note,
        "status": "open",
        "created_at": _now_iso(),
    }
    data.TASKS.append(task)
    return task


@api.post("/credits", status_code=201)
def create_credit(body: CreditIn):
    _contact_or_404(body.contact_id)
    _ticket_or_404(body.ticket_id)
    credit = {
        "credit_id": _next_id("CR", data.CREDITS),
        "contact_id": body.contact_id,
        "ticket_id": body.ticket_id,
        "amount_usd": body.amount_usd,
        "reason": body.reason,
        "status": "pending_review",
        "created_at": _now_iso(),
    }
    data.CREDITS.append(credit)
    return credit


@api.post("/tickets/{ticket_id}/escalate")
def escalate_ticket(ticket_id: str, body: EscalateIn):
    """Replace the ticket with a new dict rather than editing it in place, so
    any reference a caller still holds to the old ticket stays unchanged."""
    idx = _ticket_index_or_404(ticket_id)
    now = _now_iso()
    escalated = {
        **data.TICKETS[idx],
        "status": "escalated",
        "priority": body.priority,
        "escalation": {"reason": body.reason, "escalated_at": now},
        "updated_at": now,
    }
    data.TICKETS[idx] = escalated
    return escalated


app.include_router(api)
