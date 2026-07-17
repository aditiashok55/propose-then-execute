# propose-then-execute

A toy agentic loop that detects and remediates Postgres connection-pool
exhaustion — and a worked example of fixing **excessive agency**
(OWASP LLM06) in that same loop, by inserting a deterministic policy
gate between the model's decision and anything irreversible happening.

Originally built as `pg-loop-demo` to demonstrate "loop engineering."
This version keeps that same loop and adds the propose/gate/execute
split on top of it, so the before/after is visible in one repo instead
of two.

## The problem this demonstrates

The first version of this loop had one guardrail: the harness decided
`dry_run`, not the model, gated behind a `--live` flag. That's a single
binary gate — either the model's decision executes, or it doesn't.
Nothing checks the *decision itself*. An LLM reading ambiguous evidence
(`idle in transaction`, some duration, no context on what the session
actually is) had a straight path to `pg_terminate_backend()` the moment
`--live` was on.

That's excessive agency in miniature: excessive **functionality** (a
termination tool with no independent safety check), excessive
**permissions** (scoped to "any session matching a pattern," not "only
sessions confirmed safe"), and excessive **autonomy** (decision and
execution in the same turn, no checkpoint between them).

## The fix: propose, gate, execute

```
model proposes  -->  loop/policy_gate.py  -->  harness executes
(dry-run only,       (pure functions, zero      (only path that can
 always)              model calls, testable      call execute_pool_action,
                       without Postgres)          and only after approval)
```

Three concrete changes from the original loop:

1. **`apply_pool_action` is now always a proposal.** `--live` no longer
   flips `dry_run` through to the database — it only controls whether
   the *harness* is allowed to act on an approved proposal afterward.
2. **`loop/policy_gate.py` is the actual gate.** Static rules, no
   model calls, no I/O: a hard floor on idle duration (independent of
   whatever threshold the model asks for), a query-text allowlist for
   known long-running jobs, and a per-iteration termination cap. Fully
   unit-tested in `tests/test_policy_gate.py` — runs in milliseconds,
   no database needed.
3. **`execute_pool_action` is invisible to the model.** It's a real
   MCP tool, but `MODEL_FACING_TOOLS` in `harness.py` filters it out of
   the function declarations sent to Gemini before the model ever sees
   the tool list. This isn't a prompt telling the model not to call it
   — the model has no function signature for it, so it structurally
   cannot. Only harness code calls it, and only after the gate approves
   specific pids.

## Why this scenario

Connection pool exhaustion is a real, common failure mode with a clean
numeric goal condition, which is what makes a loop demonstrable and
trustworthy in the first place:

```
goal: (active_connections / max_connections) < 0.8
      sustained across 2 consecutive checks after an action is taken
```

## Architecture (mapped to loop engineering anatomy)

| Loop engineering concept | This project |
|---|---|
| **Trigger** | `harness.py` polls connection stats; loop starts when ratio > 0.8 |
| **Tools / connectors** | `mcp_server/server.py` — MCP server exposing read tools, a proposal-only write tool, and a gate-only execution tool |
| **Act (propose)** | Model calls `apply_pool_action`, always dry-run — returns candidates, terminates nothing |
| **Gate** | `loop/policy_gate.py` evaluates candidates against static rules, model-blind |
| **Act (execute)** | Harness — never the model — calls `execute_pool_action` with only the approved pids |
| **Observe** | Harness re-reads connection stats after every action |
| **Verify** | `loop/verifier.py` — pure function checking the goal condition against history |
| **Stop rules / guardrails** | Max iterations, always-dry-run proposals, model-blind execution tool, gate rules independent of model input |
| **External state** | `transcript.json` — full loop history including `policy_gate` decisions per iteration |

```
seed_failure.py --> Postgres <-- mcp_server/server.py (MCP tools)
                                        ^
                                        | stdio (MCP protocol)
                                        v
                                loop/harness.py
                       model sees: get_connection_stats,
                                   get_slow_queries,
                                   apply_pool_action (proposal only)
                       model never sees: execute_pool_action
                                        |
                                        v
                             loop/policy_gate.py (deterministic)
                                        |
                          only if approved --> execute_pool_action
```

## What this demo intentionally does NOT do

- It doesn't touch `max_connections` (requires a Postgres restart; out
  of scope for a live remediation loop).
- The policy gate's rules are illustrative, not production-grade — a
  real deployment would load the allowlist from config or a table, not
  a hardcoded list in Python.
- It doesn't handle multiple simultaneous failure types — one scenario,
  done honestly, beats five done superficially.
- It has no retry/backoff sophistication. The loop stays simple so the
  propose/gate/execute seam is visible, not buried under production
  hardening.

## Setup

```bash
pip install -r requirements.txt

# Postgres via Docker (throwaway, for the demo)
docker run --name loopdemo-pg \
  -e POSTGRES_PASSWORD=loopdemo \
  -e POSTGRES_DB=loopdemo \
  -p 5432:5432 \
  -d postgres:16

cp .env.example .env
# edit .env:
#   DATABASE_URL=postgresql://postgres:loopdemo@localhost:5432/loopdemo
#   GEMINI_API_KEY=... (free at https://aistudio.google.com -> Get API key)
```

## Running the demo

**Terminal 1 — seed the failure** (opens N idle-in-transaction connections):

```bash
python seed/seed_failure.py --connections 15 --hold-seconds 120
```

**Terminal 2 — run the loop** (must run as a module, not a script,
so the `loop`/`mcp_server` packages resolve correctly):

```bash
# from the repo root
python -m loop.harness            # dry-run by default
python -m loop.harness --live     # lets the gate actually execute approved terminations
```

Each run writes a full transcript to `transcript.json`, including a
`policy_gate` block per iteration — proposed pids, approved pids,
rejected pids with reasons, and whether execution actually happened.
This is the file worth pulling for article screenshots.

## Tests

```bash
python -m pytest tests/test_policy_gate.py -v
```

No Postgres, no model, no network — just proof that the gate rejects
known-safe query patterns, enforces the idle-duration floor regardless
of what threshold the model requests, and caps terminations per
iteration. This is deliberately the easiest part of the whole system
to verify in isolation.

## Files

- `mcp_server/db.py` — asyncpg connection pool, the underlying
  queries, and `terminate_by_pids` — a dumb executor that trusts its
  input completely, on purpose, because trust is established upstream
  by the gate, not by this function.
- `mcp_server/server.py` — FastMCP server exposing `get_connection_stats`,
  `get_slow_queries`, `apply_pool_action` (proposal only), and
  `execute_pool_action` (harness-only, never model-facing).
- `loop/policy_gate.py` — the deterministic gate: `Proposal` in,
  `PolicyDecision` out, zero model calls.
- `loop/verifier.py` — the goal-condition check, kept separate so it's
  easy to point to as "the thing that decides when to stop."
- `loop/harness.py` — the loop: starts the MCP server as a subprocess,
  filters the tool list down to `MODEL_FACING_TOOLS` before calling
  Gemini, runs proposals through the policy gate, and is the only
  caller of `execute_pool_action`. Model defaults to
  `gemini-2.5-flash`; override with `GEMINI_MODEL` in `.env`.
- `seed/seed_failure.py` — opens and holds N idle-in-transaction
  connections to simulate the failure.
- `tests/test_policy_gate.py` — unit tests proving the gate's behavior
  without needing Postgres or a model.