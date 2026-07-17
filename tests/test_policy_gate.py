"""
Proves the batch-job scenario from the article: a connection that's
idle-in-transaction for a long time but is actually a known nightly
job gets rejected by the gate, with no Postgres or model involved.
"""

from loop.policy_gate import (
    MAX_TERMINATIONS_PER_ITERATION,
    MIN_IDLE_SECONDS,
    Proposal,
    evaluate,
)


def test_known_batch_job_is_rejected_despite_matching_idle_pattern():
    proposals = [
        Proposal(pid=4521, idle_seconds=340.0, query_text="CALL nightly_batch_reconcile()"),
    ]
    decision = evaluate(proposals)

    assert decision.approved_pids == []
    assert decision.rejected[0]["pid"] == 4521
    assert "known-safe pattern" in decision.rejected[0]["reason"]


def test_genuinely_leaked_connection_is_approved():
    proposals = [
        Proposal(pid=9001, idle_seconds=180.0, query_text="UPDATE orders SET status = $1"),
    ]
    decision = evaluate(proposals)

    assert decision.approved_pids == [9001]
    assert decision.rejected == []


def test_below_idle_floor_is_rejected_even_if_agent_asked_for_lower_threshold():
    proposals = [Proposal(pid=1, idle_seconds=MIN_IDLE_SECONDS - 1, query_text="SELECT 1")]
    decision = evaluate(proposals)

    assert decision.approved_pids == []
    assert "below floor" in decision.rejected[0]["reason"]


def test_termination_cap_holds_regardless_of_how_many_are_proposed():
    proposals = [
        Proposal(pid=i, idle_seconds=200.0, query_text="SELECT 1")
        for i in range(MAX_TERMINATIONS_PER_ITERATION + 5)
    ]
    decision = evaluate(proposals)

    assert len(decision.approved_pids) == MAX_TERMINATIONS_PER_ITERATION
    assert any(r["reason"] == "iteration_termination_cap_reached" for r in decision.rejected)