"""Shared fixtures: isolate mutable module state so tests never leak into
each other (tokens issued, records created, pending previews)."""

import pytest

from mock_helpdesk import app as mock_app
from mock_helpdesk import data


@pytest.fixture(autouse=True)
def isolated_state():
    snapshots = {
        name: list(getattr(data, name))
        for name in ("CONTACTS", "TICKETS", "TASKS", "CREDITS", "REPLIES")
    }
    tokens_before = dict(mock_app._tokens)
    yield
    for name, rows in snapshots.items():
        getattr(data, name)[:] = rows
    mock_app._tokens.clear()
    mock_app._tokens.update(tokens_before)
