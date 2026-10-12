"""Helpdesk/CRM API client with OAuth 2.0 client_credentials token caching.

The same discipline as a production integration client:
- fetch a token once, cache it, refresh 60s before expiry;
- on a 401, invalidate the cached token and retry once (server-side token
  stores get cycled; a client that trusts its cache until local expiry
  locks itself out for the rest of the hour);
- surface HTTP and transport errors as structured results instead of
  raising through the agent (tools should return data the model can
  reason about, never a stack trace).

Every method returns the decoded JSON body on success, or an error dict
{"error": True, "status_code": int | None, "detail": str} on failure.
"""

import os
import time
from urllib.parse import quote

import httpx


class HelpdeskClient:
    def __init__(self, http_client: httpx.Client | None = None):
        self.base_url = os.environ.get("HELPDESK_BASE_URL", "http://127.0.0.1:8086")
        self.client_id = os.environ.get("HELPDESK_CLIENT_ID", "demo-client")
        self.client_secret = os.environ.get("HELPDESK_CLIENT_SECRET", "demo-secret")
        self._http = http_client or httpx.Client(base_url=self.base_url, timeout=10)
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth ------------------------------------------------------------
    def invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self._http.post(
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + payload.get("expires_in", 3600)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # -- request core ----------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        """One retry on 401 with a fresh token; structured errors otherwise."""
        for attempt in (1, 2):
            try:
                headers = self._headers()
            except httpx.HTTPError as exc:
                return {
                    "error": True,
                    "status_code": None,
                    "detail": f"token acquisition failed: {exc}",
                }
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                return {
                    "error": True,
                    "status_code": None,
                    "detail": f"transport error: {exc}",
                }
            if resp.status_code == 401 and attempt == 1:
                # Server no longer honors our cached token: refresh and retry.
                self.invalidate_token()
                continue
            if resp.status_code >= 400:
                return {
                    "error": True,
                    "status_code": resp.status_code,
                    "detail": resp.text,
                }
            return resp.json()
        return {"error": True, "status_code": None, "detail": "retry exhausted"}

    @staticmethod
    def _segment(value: str) -> str:
        """Ids come from model output; quote them so a stray '/' or '?' cannot
        change which route is hit."""
        return quote(str(value), safe="")

    # -- reads ------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/api/v1/health")

    def list_tickets(
        self,
        status: str | None = None,
        contact_id: str | None = None,
        q: str | None = None,
    ) -> dict:
        filters = {"status": status, "contact_id": contact_id, "q": q}
        params = {key: value for key, value in filters.items() if value is not None}
        return self._request("GET", "/api/v1/tickets", params=params)

    def get_ticket(self, ticket_id: str) -> dict:
        return self._request("GET", f"/api/v1/tickets/{self._segment(ticket_id)}")

    def find_contacts(self, email: str | None = None) -> dict:
        params = {"email": email} if email is not None else {}
        return self._request("GET", "/api/v1/contacts", params=params)

    def get_contact(self, contact_id: str) -> dict:
        return self._request("GET", f"/api/v1/contacts/{self._segment(contact_id)}")

    # -- writes (each one is gated by triage_agent/guard.py before it is called)
    def create_reply(self, ticket_id: str, text: str) -> dict:
        path = f"/api/v1/tickets/{self._segment(ticket_id)}/replies"
        return self._request("POST", path, json={"text": text})

    def create_task(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/tasks", json=payload)

    def create_credit(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/credits", json=payload)

    def escalate_ticket(self, ticket_id: str, payload: dict) -> dict:
        path = f"/api/v1/tickets/{self._segment(ticket_id)}/escalate"
        return self._request("POST", path, json=payload)


_client: HelpdeskClient | None = None


def get_client() -> HelpdeskClient:
    """Module-level singleton so tools share one token cache.
    Tests replace it via set_client()."""
    global _client
    if _client is None:
        _client = HelpdeskClient()
    return _client


def set_client(client: HelpdeskClient | None) -> None:
    global _client
    _client = client
