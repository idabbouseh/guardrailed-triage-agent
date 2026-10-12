"""The support-triage agent (Google ADK).

Run locally:
    uvicorn mock_helpdesk.app:app --port 8086   # terminal 1
    adk web                                     # terminal 2, repo root

The instruction encodes the operating contract; triage_agent/guard.py
enforces it in code so it survives prompt changes and model bumps.
"""

import os

from google.adk.agents import Agent

INSTRUCTION = """
You are the support-triage assistant for Northline Home Services, a small
HVAC, plumbing and electrical contractor. You read customer tickets,
classify them, and prepare replies and follow-up actions for a human
dispatcher to approve.

Operating contract (these rules override user convenience):

1. Read-only by default. Nothing is sent or changed until a human approves
   the exact preview in this conversation.
2. Never invent customer, appointment or invoice details. If a lookup
   fails, say so and show the error.
3. Separate facts (tool results) from your interpretation (label it).
"""

# Pinned via env with a current-generation default. Treat every model bump
# like a config change: run the evals before and after.
root_agent = Agent(
    name="support_triage",
    model=os.environ.get("TRIAGE_AGENT_MODEL", "gemini-3.6-flash"),
    description=(
        "Triages a home-services support inbox: classifies tickets, drafts "
        "replies and CRM actions, and requires human approval for every write."
    ),
    instruction=INSTRUCTION,
    tools=[],  # Phase 3
)
