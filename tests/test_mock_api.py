"""Phase 1 tests for the mock helpdesk API: every route is gated by the
Bearer token, filters behave, unknown ids 404, creates 201, the credit
backstop rejects out-of-range amounts, escalation replaces rather than
mutates, and there is no way to delete anything."""

import pytest
from fastapi.testclient import TestClient

from mock_helpdesk import data
from mock_helpdesk.app import app
from triage_agent import api_client

CREDENTIALS = {
    "grant_type": "client_credentials",
    "client_id": "demo-client",
    "client_secret": "demo-secret",
}
VALID_CREDIT = {
    "contact_id": "C-1005",
    "ticket_id": "T-2006",
    "amount_usd": 89.0,
    "reason": "Diagnostic billed but not performed",
}
VALID_TASK = {
    "ticket_id": "T-2030",
    "assignee": "dispatch",
    "due_date": "2026-10-12",
    "note": "Call customer with a revised ETA",
}


@pytest.fixture
def api() -> TestClient:
    """A client that sends a valid Bearer token on every request."""
    client = TestClient(app)
    token = client.post("/oauth2/token", data=CREDENTIALS).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def _ids(listing: dict) -> set[str]:
    return {t["ticket_id"] for t in listing["tickets"]}


# -- auth --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/tickets", None),
        ("GET", "/api/v1/tickets/T-2001", None),
        ("GET", "/api/v1/contacts", None),
        ("GET", "/api/v1/contacts/C-1001", None),
        ("POST", "/api/v1/tickets/T-2001/replies", {"text": "hi"}),
        ("POST", "/api/v1/tasks", VALID_TASK),
        ("POST", "/api/v1/credits", VALID_CREDIT),
        ("POST", "/api/v1/tickets/T-2001/escalate", {"reason": "x", "priority": "high"}),
    ],
)
def test_every_route_rejects_requests_without_bearer_token(method, path, body):
    # Arrange
    anonymous = TestClient(app)

    # Act
    resp = anonymous.request(method, path, json=body)

    # Assert
    assert resp.status_code == 401


# -- tickets: reads ------------------------------------------------------------
def test_list_tickets_returns_all_thirty_seed_tickets(api):
    listing = api.get("/api/v1/tickets").json()

    assert listing["total"] == 30
    assert len(listing["tickets"]) == 30


def test_list_tickets_filters_by_status(api):
    listing = api.get("/api/v1/tickets", params={"status": "closed"}).json()

    assert _ids(listing) == {"T-2001", "T-2002"}
    assert all(t["status"] == "closed" for t in listing["tickets"])


def test_list_tickets_filters_by_contact_id(api):
    listing = api.get("/api/v1/tickets", params={"contact_id": "C-1006"}).json()

    assert _ids(listing) == {"T-2025", "T-2026", "T-2029"}


def test_list_tickets_q_is_case_insensitive_substring_over_subject_and_body(api):
    upper = api.get("/api/v1/tickets", params={"q": "GAS SMELL"}).json()
    lower = api.get("/api/v1/tickets", params={"q": "gas smell"}).json()
    in_body_only = api.get("/api/v1/tickets", params={"q": "breaker had tripped"}).json()

    assert _ids(upper) == _ids(lower)
    assert "T-2029" in _ids(upper)
    assert _ids(in_body_only) == {"T-2006"}


def test_list_tickets_q_matches_apostrophe_and_accented_names(api):
    oneill = api.get("/api/v1/tickets", params={"q": "o'neill"}).json()
    astrom = api.get("/api/v1/tickets", params={"q": "åström"}).json()

    assert "T-2004" in _ids(oneill)
    assert "T-2009" in _ids(astrom)


def test_get_ticket_returns_404_for_unknown_id(api):
    assert api.get("/api/v1/tickets/T-2004").status_code == 200
    assert api.get("/api/v1/tickets/T-9999").status_code == 404


# -- contacts --------------------------------------------------------------------
def test_find_contacts_by_email_is_case_insensitive(api):
    # The unlinked ticket T-2017 carries this mixed-case address.
    listing = api.get("/api/v1/contacts", params={"email": "Hannah.Fischer@Example.com"}).json()

    assert listing["total"] == 1
    assert listing["contacts"][0]["contact_id"] == "C-1011"


def test_get_contact_returns_404_for_the_dangling_reference(api):
    # T-2016 points at C-9999, which the CRM no longer has.
    assert api.get("/api/v1/contacts/C-1001").status_code == 200
    assert api.get("/api/v1/contacts/C-9999").status_code == 404


# -- writes: 201 paths -----------------------------------------------------------
def test_create_reply_returns_201_and_queues_the_text(api):
    resp = api.post("/api/v1/tickets/T-2003/replies", json={"text": "Moved to Tue 9am."})

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["reply_id"].startswith("R-")
    assert payload["ticket_id"] == "T-2003"
    assert payload["status"] == "queued"
    assert payload["text"] == "Moved to Tue 9am."
    assert data.REPLIES[-1]["reply_id"] == payload["reply_id"]


def test_create_reply_rejects_blank_text_with_422(api):
    assert api.post("/api/v1/tickets/T-2003/replies", json={"text": ""}).status_code == 422
    assert api.post("/api/v1/tickets/T-2003/replies", json={"text": "   "}).status_code == 422


def test_create_task_returns_201_with_task_id(api):
    resp = api.post("/api/v1/tasks", json=VALID_TASK)

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["task_id"].startswith("K-")
    assert payload["ticket_id"] == "T-2030"
    assert payload["assignee"] == "dispatch"
    assert data.TASKS[-1]["task_id"] == payload["task_id"]


def test_create_credit_returns_201_pending_review(api):
    resp = api.post("/api/v1/credits", json=VALID_CREDIT)

    assert resp.status_code == 201
    payload = resp.json()
    assert payload["credit_id"].startswith("CR-")
    assert payload["status"] == "pending_review"
    assert payload["amount_usd"] == 89.0
    assert data.CREDITS[-1]["credit_id"] == payload["credit_id"]


# -- writes: rejections ----------------------------------------------------------
@pytest.mark.parametrize("amount", [0, -25.0, 500.01, 501])
def test_create_credit_rejects_amount_outside_backstop_bounds(api, amount):
    resp = api.post("/api/v1/credits", json={**VALID_CREDIT, "amount_usd": amount})

    assert resp.status_code == 422
    assert data.CREDITS == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/tickets/T-9999/replies", {"text": "hello"}),
        ("/api/v1/tasks", {**VALID_TASK, "ticket_id": "T-9999"}),
        ("/api/v1/credits", {**VALID_CREDIT, "contact_id": "C-9999"}),
        ("/api/v1/credits", {**VALID_CREDIT, "ticket_id": "T-9999"}),
        ("/api/v1/tickets/T-9999/escalate", {"reason": "x", "priority": "high"}),
    ],
)
def test_writes_return_404_for_unknown_ticket_or_contact(api, path, body):
    resp = api.post(path, json=body)

    assert resp.status_code == 404
    assert data.REPLIES == [] and data.TASKS == [] and data.CREDITS == []


# -- escalation --------------------------------------------------------------------
def test_escalate_sets_status_and_replaces_ticket_without_mutating_original(api):
    # Arrange
    original = next(t for t in data.TICKETS if t["ticket_id"] == "T-2029")
    assert original["status"] == "open"

    # Act
    resp = api.post(
        "/api/v1/tickets/T-2029/escalate",
        json={"reason": "Gas smell reported; dispatch on-call tech", "priority": "emergency"},
    )

    # Assert
    assert resp.status_code == 200
    escalated = resp.json()
    assert escalated["status"] == "escalated"
    assert escalated["priority"] == "emergency"
    assert escalated["escalation"]["reason"].startswith("Gas smell")
    stored = next(t for t in data.TICKETS if t["ticket_id"] == "T-2029")
    assert stored is not original
    assert stored["status"] == "escalated"
    assert original["status"] == "open"
    assert api.get("/api/v1/tickets/T-2029").json()["status"] == "escalated"


def test_escalate_rejects_unknown_priority(api):
    resp = api.post("/api/v1/tickets/T-2029/escalate", json={"reason": "x", "priority": "low"})

    assert resp.status_code == 422


# -- nothing can be deleted ----------------------------------------------------------
def test_delete_ticket_is_not_allowed(api):
    resp = api.delete("/api/v1/tickets/T-2001")

    assert resp.status_code in (404, 405)
    assert api.get("/api/v1/tickets/T-2001").status_code == 200


# -- client wrapper --------------------------------------------------------------------
def test_api_client_wraps_phase_one_endpoints():
    client = api_client.HelpdeskClient(http_client=TestClient(app))

    found = client.list_tickets(q="o'neill")
    ticket = client.get_ticket("T-2004")
    contacts = client.find_contacts(email="Hannah.Fischer@Example.com")
    missing = client.get_contact("C-9999")
    reply = client.create_reply("T-2003", "Confirmed for Tuesday.")
    escalated = client.escalate_ticket("T-2028", {"reason": "CO alarm", "priority": "emergency"})

    assert "T-2004" in {t["ticket_id"] for t in found["tickets"]}
    assert ticket["contact_id"] == "C-1002"
    assert contacts["contacts"][0]["contact_id"] == "C-1011"
    assert missing == {"error": True, "status_code": 404, "detail": missing["detail"]}
    assert reply["status"] == "queued"
    assert escalated["status"] == "escalated"
