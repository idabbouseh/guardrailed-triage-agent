# Guardrailed Triage Agent

A Google ADK agent that works a small business's support inbox and cannot
change anything without a human approving the exact payload first.

Work in progress.

```bash
py -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
```
