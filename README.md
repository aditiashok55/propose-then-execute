# pg-loop-demo

A toy agentic loop that detects and remediates Postgres connection-pool
exhaustion, built to demonstrate "loop engineering" on top of a real
MCP server rather than a coding agent.

## Why this scenario

Connection pool exhaustion is a real, common failure mode, and it has a
clean numeric goal condition — which is exactly what a loop needs to be
demonstrable and trustworthy:

```
goal: (active_connections / max_connections) < 0.8
      sustained across 2 consecutive checks after an action is taken
```

## Architecture (mapped to loop engineering anatomy)

| Loop engineering concept | This project |
|---|---|
| **Trigger** | `harness.py` polls connection stats; loop starts when ratio > 0.8 |
| **Tools / connectors** | `mcp_server/server.py` — an MCP server exposing read + write tools over Postgres |
| **Act** | Claude calls a tool (e.g. kill idle-in-transaction sessions) |
| **Observe** | Harness re-reads connection stats after every action |
| **Verify** | `loop/verifier.py` — pure function checking the goal condition against history |
| **Stop rules / guardrails** | Max iterations, dry-run default, explicit approval gate for writes |
| **External state** | `transcript.json` — full loop history, written after every cycle |

```
seed_failure.py  -->  Postgres  <--  mcp_server/server.py (MCP tools)
                                            ^
                                            | stdio (MCP protocol)
                                            v
                                    loop/harness.py
                                    (Claude + verifier + guardrails)
```

## What this demo intentionally does NOT do

Worth being upfront about this — it's also the most interesting part to
write about:

- It doesn't touch `max_connections` (that requires a Postgres restart;
  out of scope for a live remediation loop).
- It doesn't auto-apply writes by default. `apply_pool_action` runs in
  **dry-run mode unless you pass `--live`**, and even in live mode it
  logs the exact statement it would run before running it.
- It doesn't handle multiple simultaneous failure types — one scenario,
  done honestly, beats five scenarios done superficially.
- It has no retry/backoff sophistication. The loop is intentionally
  simple so the *shape* of loop engineering (trigger → act → observe →
  verify → stop) is visible, not buried under production hardening.

## Setup

```bash
pip install -r requirements.txt

# create a local throwaway database
createdb loopdemo

cp .env.example .env
# edit .env: set DATABASE_URL and GEMINI_API_KEY (get one free at
# https://aistudio.google.com -> Get API key)
```

## Running the demo

**Terminal 1 — seed the failure** (opens N idle-in-transaction connections):

```bash
python seed/seed_failure.py --connections 15 --hold-seconds 120
```

**Terminal 2 — run the loop** (dry-run by default):

```bash
python loop/harness.py
```

Add `--live` to actually let the agent terminate idle sessions:

```bash
python loop/harness.py --live
```

Each run writes a full transcript to `transcript.json` — this is the
file worth pulling quotes/screenshots from for the article. It contains
every observation, every tool call, every verifier decision, and the
stop reason.

## Files

- `mcp_server/db.py` — asyncpg connection pool + the three underlying
  queries/actions
- `mcp_server/server.py` — FastMCP server exposing `get_connection_stats`,
  `get_slow_queries`, `apply_pool_action`
- `loop/verifier.py` — the goal-condition check, kept separate from the
  loop so it's easy to point to in the article as "the thing that
  decides when to stop"
- `loop/harness.py` — the actual loop: starts the MCP server as a
  subprocess, talks to it over stdio, calls Gemini with the MCP tools
  converted to its function-declaration schema, executes tool calls,
  checks the verifier, repeats or stops. Model defaults to
  `gemini-2.5-flash`; override with `GEMINI_MODEL` in `.env`.
- `seed/seed_failure.py` — opens and holds N idle-in-transaction
  connections to simulate the failure
