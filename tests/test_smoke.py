"""Phase 0 smoke tests: the mock issues tokens, rejects bad credentials, and
the client's cached-token recovery path works against the in-process app."""

from fastapi.testclient import TestClient

from mock_helpdesk import app as mock_app
from mock_helpdesk.app import app
from triage_agent import api_client


def _token(client: TestClient, secret: str = "demo-secret"):
    return client.post(
        "/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "demo-client",
            "client_secret": secret,
        },
    )


def test_token_issued_for_valid_credentials():
    resp = _token(TestClient(app))
    assert resp.status_code == 200
    assert resp.json()["token_type"] == "Bearer"


def test_token_rejected_for_bad_secret():
    assert _token(TestClient(app), secret="wrong").status_code == 401


def test_health_requires_bearer_token():
    assert TestClient(app).get("/api/v1/health").status_code == 401


def test_client_recovers_after_server_side_token_cycle():
    client = api_client.HelpdeskClient(http_client=TestClient(app))
    assert client.health()["status"] == "ok"
    mock_app._tokens.clear()  # server restarted, cached token now invalid
    result = client.health()
    assert result.get("error") is not True
    assert result["status"] == "ok"
