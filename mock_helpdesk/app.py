"""Mock helpdesk + CRM API for a small home-services business.

Shaped like the SaaS APIs a small business actually runs on:
OAuth 2.0 client_credentials at POST /oauth2/token, Bearer token on every
call, 404s on unknown ids, 201s on creates, and no delete endpoints at all
(the agent cannot destroy what the API cannot destroy).

Run:  uvicorn mock_helpdesk.app:app --port 8086
"""

import os
import secrets
import time

from fastapi import Depends, FastAPI, Form, Header, HTTPException

from . import data

CLIENT_ID = os.environ.get("HELPDESK_CLIENT_ID", "demo-client")
CLIENT_SECRET = os.environ.get("HELPDESK_CLIENT_SECRET", "demo-secret")
TOKEN_TTL_SECONDS = 3600

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


@app.get("/api/v1/health", dependencies=[Depends(require_auth)])
def health():
    return {
        "status": "ok",
        "tickets": len(data.TICKETS),
        "contacts": len(data.CONTACTS),
    }
