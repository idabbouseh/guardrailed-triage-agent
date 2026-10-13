"""Run the triage agent over the seeded tickets and score it.

    python -m evals.run_evals [--model NAME] [--cases evals/cases.jsonl]
                              [--limit N] [--only T-2029,T-2012]
                              [--out evals/latest-scorecard.md]
                              [--json evals/latest-results.json] [--dry-run]

No helpdesk server is needed: the agent's API client is bound to an
in-process FastAPI TestClient of mock_helpdesk.app. Each case starts from
the pristine seed data, a fresh preview registry and a fresh ADK session,
then "Triage ticket <id>." goes through the ADK Runner. The harness never
approves anything, so any confirmed write the model attempts is scored as
a guardrail breach. Every tool decision lands in evals/audit-latest.jsonl.

Exit codes: 0 run complete (or dry-run ok), 1 wiring/usage error,
2 GOOGLE_API_KEY missing.
"""

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from google.adk import version as adk_version
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from evals import scorer
from mock_helpdesk import data
from mock_helpdesk.app import app
from triage_agent import api_client, guard
from triage_agent.agent import root_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "evals"
DEFAULT_CASES = EVALS_DIR / "cases.jsonl"
DEFAULT_OUT = EVALS_DIR / "latest-scorecard.md"
DEFAULT_JSON = EVALS_DIR / "latest-results.json"
AUDIT_PATH = EVALS_DIR / "audit-latest.jsonl"
APP_NAME = "triage_evals"
USER_ID = "eval-harness"
EXPECTED_TOOL_COUNT = 8
STORES = ("CONTACTS", "TICKETS", "TASKS", "CREDITS", "REPLIES")
TRUTHY = frozenset({"1", "true", "yes"})
EXIT_OK, EXIT_USAGE, EXIT_NO_KEY = 0, 1, 2
RATE_LIMIT_MARKERS = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")  # transient: quota or capacity
RATE_LIMIT_BACKOFF_SECONDS = 65
CASE_ENUMS = (("expected_category", scorer.CATEGORIES), ("expected_action", scorer.ACTIONS))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals", description=__doc__)
    parser.add_argument("--model", help="model name; else TRIAGE_AGENT_MODEL, else the agent default")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, help="run only the first N selected cases")
    parser.add_argument("--only", help="comma-separated ticket ids to run")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="scorecard markdown path")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON, help="raw results path")
    parser.add_argument("--dry-run", action="store_true", help="validate wiring, call no model")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds to wait between cases (free-tier keys allow ~15 requests/minute)")
    parser.add_argument("--rate-limit-retries", type=int, default=2,
                        help="re-run a case this many times if the model API returns a rate-limit or capacity error")
    return parser.parse_args(argv)


def load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def select_cases(cases: list[dict], only: str | None, limit: int | None) -> list[dict]:
    if only:
        wanted = [t.strip() for t in only.split(",") if t.strip()]
        by_id = {case["ticket_id"]: case for case in cases}
        unknown = [t for t in wanted if t not in by_id]
        if unknown:
            raise SystemExit(f"--only names tickets not in the case file: {', '.join(unknown)}")
        cases = [by_id[t] for t in wanted]
    return cases[:limit] if limit is not None else cases


def validate_cases(cases: list[dict]) -> None:
    seeded = {t["ticket_id"] for t in data.TICKETS}
    for case in cases:
        ticket_id = case.get("ticket_id")
        if ticket_id not in seeded:
            raise SystemExit(f"case {ticket_id!r} is not a seeded ticket")
        for field, allowed in CASE_ENUMS:
            if case.get(field) not in allowed:
                raise SystemExit(f"{ticket_id}: bad {field} {case.get(field)!r}")


def wire_mock() -> api_client.HelpdeskClient:
    """Bind the agent's API client to the in-process mock; returns the client."""
    client = api_client.HelpdeskClient(http_client=TestClient(app))
    api_client.set_client(client)
    return client


def unwire() -> None:
    api_client.set_client(None)
    guard.set_registry(None)


def reset_audit_log(path: Path = AUDIT_PATH) -> None:
    os.environ["AUDIT_LOG_PATH"] = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def resolve_model(cli_model: str | None) -> str:
    return cli_model or os.environ.get("TRIAGE_AGENT_MODEL") or str(root_agent.model)


def build_agent(model):
    """The repo's root_agent with ``model`` swapped in (a name or a BaseLlm)."""
    return root_agent if model == root_agent.model else root_agent.model_copy(update={"model": model})


def build_runner(agent) -> tuple[Runner, InMemorySessionService]:
    session_service = InMemorySessionService()
    return Runner(agent=agent, app_name=APP_NAME, session_service=session_service), session_service


def has_model_credentials() -> bool:
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in TRUTHY
    return use_vertex or bool(os.environ.get("GOOGLE_API_KEY"))


def snapshot_stores() -> dict:
    return {name: copy.deepcopy(getattr(data, name)) for name in STORES}


def restore_stores(snapshot: dict) -> None:
    for name, rows in snapshot.items():
        getattr(data, name)[:] = copy.deepcopy(rows)


def collect_events(events) -> tuple[str, list[dict]]:
    """Fold Runner events into (final_text, tool_calls). Function responses
    are matched to their call by id, falling back to the first unanswered
    call of the same name. Model "thought" parts are not text."""
    texts: list[str] = []
    calls: list[dict] = []
    for event in events:
        for call in event.get_function_calls():
            calls.append({"id": call.id, "name": call.name, "args": dict(call.args or {}), "result": None})
        for response in event.get_function_responses():
            target = next((c for c in calls if c["id"] is not None and c["id"] == response.id), None) \
                or next((c for c in calls if c["name"] == response.name and c["result"] is None), None)
            if target is not None:
                target["result"] = response.response
        if event.author != "user" and not event.partial and event.content and event.content.parts:
            texts.extend(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
    tool_calls = [{"name": c["name"], "args": c["args"], "result": c["result"]} for c in calls]
    return "\n".join(texts).strip(), tool_calls


def run_case(runner: Runner, session_service: InMemorySessionService, ticket_id: str,
             attempt: int = 0) -> dict:
    """Fresh registry, fresh session, one user turn; data stores restored
    afterwards. A runner exception is captured as runner_error so the
    scorecard still shows what happened."""
    snapshot = snapshot_stores()
    guard.set_registry(guard.PreviewRegistry())
    suffix = f"-retry{attempt}" if attempt else ""
    session = session_service.create_session_sync(
        app_name=APP_NAME, user_id=USER_ID, session_id=f"eval-{ticket_id}{suffix}"
    )
    message = types.Content(role="user", parts=[types.Part.from_text(text=f"Triage ticket {ticket_id}.")])
    events: list = []
    runner_error: str | None = None
    try:
        for event in runner.run(user_id=USER_ID, session_id=session.id, new_message=message):
            events.append(event)
        final_text, tool_calls = collect_events(events)
    except Exception as exc:  # noqa: BLE001 - recorded in the transcript, not hidden
        partial_text, tool_calls = collect_events(events)
        final_text = f"{partial_text}\n[runner error] {type(exc).__name__}: {exc}".strip()
        runner_error = f"{type(exc).__name__}: {exc}"
    finally:
        restore_stores(snapshot)
    return {
        "ticket_id": ticket_id,
        "final_text": final_text,
        "tool_calls": tool_calls,
        "runner_error": runner_error,
    }


def write_outputs(args: argparse.Namespace, model: str, results: list[dict], transcripts: list[dict]) -> None:
    meta = {
        "model": model, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases": len(results), "cases_file": str(args.cases), "only": args.only, "limit": args.limit,
        "adk_version": getattr(adk_version, "__version__", "unknown"), "audit_log": str(AUDIT_PATH),
    }
    totals = scorer.aggregate(results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(scorer.render_scorecard(meta, results, totals), encoding="utf-8")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "totals": totals,
               "results": [{**r, "transcript": t} for r, t in zip(results, transcripts)]}
    args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"done: {totals['n']} cases, pass rate {totals['pass_rate'] * 100:.1f}%, "
          f"{totals['violation_count']} violations; scorecard -> {args.out}, results -> {args.json}")


def dry_run(args: argparse.Namespace, cases: list[dict], model: str, client) -> int:
    health = client.health()
    if health.get("error"):
        print(f"dry-run FAILED: mock helpdesk unreachable: {health.get('detail')}")
        return EXIT_USAGE
    agent = build_agent(model)
    if len(agent.tools) != EXPECTED_TOOL_COUNT:
        print(f"dry-run FAILED: agent has {len(agent.tools)} tools, expected {EXPECTED_TOOL_COUNT}")
        return EXIT_USAGE
    build_runner(agent)
    print(f"dry-run ok: agent '{agent.name}' loads with {len(agent.tools)} tools, model {model}, "
          f"{len(cases)} cases reference seeded tickets, mock reachable ({health['tickets']} tickets); "
          f"scorecard -> {args.out}, results -> {args.json}, audit -> {AUDIT_PATH}")
    return EXIT_OK


def is_rate_limited(transcript: dict) -> bool:
    error = transcript.get("runner_error") or ""
    return any(marker in error for marker in RATE_LIMIT_MARKERS)


def run_case_with_retries(runner: Runner, session_service: InMemorySessionService,
                          ticket_id: str, retries: int) -> dict:
    """A rate-limit error says nothing about the agent, so the case is re-run
    after a back-off instead of being scored as a runner failure."""
    transcript = run_case(runner, session_service, ticket_id)
    for attempt in range(1, retries + 1):
        if not is_rate_limited(transcript):
            break
        print(f"{ticket_id}: rate limited, retrying in {RATE_LIMIT_BACKOFF_SECONDS}s", flush=True)
        time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
        transcript = run_case(runner, session_service, ticket_id, attempt)
    return transcript


def run_all(args: argparse.Namespace, cases: list[dict], model: str) -> int:
    reset_audit_log()
    runner, session_service = build_runner(build_agent(model))
    results, transcripts = [], []
    for index, case in enumerate(cases):
        if index and args.pause:
            time.sleep(args.pause)
        transcript = run_case_with_retries(runner, session_service, case["ticket_id"], args.rate_limit_retries)
        result = scorer.score_case(case, transcript)
        results.append(result)
        transcripts.append(transcript)
        verdict = "PASS" if result["passed"] else f"FAIL {result['violations'] or ''}".rstrip()
        print(f"{case['ticket_id']} {case.get('scenario', '')}: {verdict}", flush=True)
    write_outputs(args, model, results, transcripts)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    try:
        cases = select_cases(load_cases(args.cases), args.only, args.limit)
        validate_cases(cases)
    except (OSError, ValueError) as exc:
        print(f"cannot load cases from {args.cases}: {exc}")
        return EXIT_USAGE
    model = resolve_model(args.model)
    if guard.is_halted():
        print("refusing to run: the agent is halted (AGENT_HALT or HALT file is set)")
        return EXIT_USAGE
    if not args.dry_run and not has_model_credentials():
        print("GOOGLE_API_KEY is not set (copy .env.example to .env and fill it in); "
              "use --dry-run to validate wiring without a model.")
        return EXIT_NO_KEY
    client = wire_mock()
    try:
        return dry_run(args, cases, model, client) if args.dry_run else run_all(args, cases, model)
    finally:
        unwire()


if __name__ == "__main__":
    sys.exit(main())
