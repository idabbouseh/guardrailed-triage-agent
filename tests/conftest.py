"""Shared fixtures: isolate mutable module state so tests never leak into
each other (tokens issued, records created, pending previews).

The snapshot is a deep copy: the API replaces ticket dicts on escalate and
appends to the write stores, and a shallow copy would still share the row
dicts, so a test that touched one would silently change the next test's
seed."""

import copy

import pytest

from mock_helpdesk import app as mock_app
from mock_helpdesk import data

STORES = ("CONTACTS", "TICKETS", "TASKS", "CREDITS", "REPLIES")


@pytest.fixture(autouse=True)
def isolated_state():
    snapshots = {name: copy.deepcopy(getattr(data, name)) for name in STORES}
    tokens_before = dict(mock_app._tokens)
    yield
    for name, rows in snapshots.items():
        getattr(data, name)[:] = rows
    mock_app._tokens.clear()
    mock_app._tokens.update(tokens_before)
