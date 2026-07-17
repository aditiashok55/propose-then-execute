"""
The deterministic gate between "the model proposed an action" and "the
action ran." No model calls happen anywhere in this file — that's the
entire point. Every check here is a static rule, not a judgment call,
which means it can be unit-tested without touching an LLM or Postgres.

This is what turns apply_pool_action's existing dry-run output (a list
of candidate pids) into an actual decision, instead of the harness just
re-calling the same tool with dry_run=False on whatever the model said.
"""

from dataclasses import dataclass, field

# Query-text substrings that mark a session as a known long-running job
# rather than a leaked connection. In a real deployment this would be
# loaded from config or a small table, not hardcoded — the point here is
# that it's data, not a model inference.
KNOWN_SAFE_QUERY_PATTERNS = [
    "reconciliation",
    "nightly_batch",
    "etl_",
]

# Hard ceiling independent of anything the model reasons about. Even a
# fully-confident, fully-correct-looking proposal cannot terminate more
# than this many sessions in one iteration.
MAX_TERMINATIONS_PER_ITERATION = 3

# Floor on idle duration, enforced here regardless of what
# threshold_seconds the model passed to apply_pool_action.
MIN_IDLE_SECONDS = 60.0


@dataclass
class Proposal:
    """One candidate termination, built from apply_pool_action's dry-run
    output correlated with get_slow_queries for query text context."""
    pid: int
    idle_seconds: float
    query_text: str | None = None


@dataclass
class PolicyDecision:
    approved_pids: list[int] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {"pid", "reason"}


def evaluate(proposals: list[Proposal]) -> PolicyDecision:
    decision = PolicyDecision()

    for p in proposals:
        if len(decision.approved_pids) >= MAX_TERMINATIONS_PER_ITERATION:
            decision.rejected.append(
                {"pid": p.pid, "reason": "iteration_termination_cap_reached"}
            )
            continue

        if p.idle_seconds < MIN_IDLE_SECONDS:
            decision.rejected.append(
                {"pid": p.pid, "reason": f"idle_seconds below floor ({MIN_IDLE_SECONDS})"}
            )
            continue

        query_lower = (p.query_text or "").lower()
        matched = next(
            (pat for pat in KNOWN_SAFE_QUERY_PATTERNS if pat in query_lower), None
        )
        if matched:
            decision.rejected.append(
                {"pid": p.pid, "reason": f"query text matches known-safe pattern '{matched}'"}
            )
            continue

        decision.approved_pids.append(p.pid)

    return decision