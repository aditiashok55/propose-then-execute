"""
Thin data-access layer over Postgres. Kept separate from the MCP server
so the queries/actions can be unit-tested or reused without going through
the MCP protocol.
"""

import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _pool


async def get_connection_stats() -> dict:
    """Snapshot of current connection usage."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        max_connections = await conn.fetchval(
            "SELECT setting::int FROM pg_settings WHERE name = 'max_connections'"
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        idle_in_txn = await conn.fetchval(
            """
            SELECT count(*) FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'idle in transaction'
            """
        )
        active = await conn.fetchval(
            """
            SELECT count(*) FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'active'
            """
        )

    return {
        "max_connections": max_connections,
        "total_connections": total,
        "active_connections": active,
        "idle_in_transaction_connections": idle_in_txn,
        "utilization_ratio": round(total / max_connections, 3) if max_connections else None,
    }


async def get_slow_queries(min_duration_seconds: float = 5.0) -> list[dict]:
    """Currently-running queries older than the given threshold.

    Often the root cause of pile-ups: a slow query holds a transaction
    open, callers queue behind it, idle-in-transaction count climbs.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pid, state, now() - query_start AS duration, query
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state != 'idle'
              AND query_start IS NOT NULL
              AND now() - query_start > interval '1 second' * $1
            ORDER BY duration DESC
            """,
            min_duration_seconds,
        )

    return [
        {
            "pid": r["pid"],
            "state": r["state"],
            "duration_seconds": r["duration"].total_seconds(),
            "query": r["query"],
        }
        for r in rows
    ]


async def kill_idle_in_transaction(threshold_seconds: float, dry_run: bool) -> dict:
    """Terminate sessions that have been idle-in-transaction longer than
    the threshold. This is the one write action this demo supports.

    Raising max_connections is deliberately NOT implemented here — it
    requires a Postgres restart and is out of scope for a live
    remediation loop (see README "What this demo does NOT do").
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        targets = await conn.fetch(
            """
            SELECT pid, now() - state_change AS idle_duration
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'idle in transaction'
              AND now() - state_change > interval '1 second' * $1
            """,
            threshold_seconds,
        )

        if dry_run:
            return {
                "dry_run": True,
                "would_terminate": [
                    {"pid": r["pid"], "idle_seconds": r["idle_duration"].total_seconds()}
                    for r in targets
                ],
            }

        terminated = []
        for r in targets:
            ok = await conn.fetchval("SELECT pg_terminate_backend($1)", r["pid"])
            if ok:
                terminated.append(r["pid"])

        return {"dry_run": False, "terminated_pids": terminated}
