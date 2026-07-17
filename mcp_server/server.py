"""
MCP server exposing three tools over a Postgres connection-pool scenario:

  - get_connection_stats : read
  - get_slow_queries      : read
  - apply_pool_action     : write (kill_idle_in_transaction only, dry-run by default)

Run standalone for manual testing:
    python -m mcp_server.server
The loop harness instead spawns this as a subprocess and talks to it
over stdio via the MCP client.
"""

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_server import db

load_dotenv()

mcp = FastMCP("pg-loop-demo")


@mcp.tool()
async def get_connection_stats() -> dict:
    """Get current Postgres connection usage: total/active/idle-in-transaction
    connections and the utilization ratio against max_connections."""
    return await db.get_connection_stats()


@mcp.tool()
async def get_slow_queries(min_duration_seconds: float = 5.0) -> list[dict]:
    """List currently-running queries older than min_duration_seconds.
    Useful for finding the root cause behind a connection pile-up."""
    return await db.get_slow_queries(min_duration_seconds)


@mcp.tool()
async def apply_pool_action(threshold_seconds: float = 60.0, dry_run: bool = True) -> dict:
    """Terminate sessions that have been idle-in-transaction longer than
    threshold_seconds. ALWAYS dry_run=True unless explicitly told otherwise
    by the harness — this tool does not decide its own safety, the caller does."""
    return await db.kill_idle_in_transaction(threshold_seconds, dry_run)


@mcp.tool()
async def execute_pool_action(pids: list[int]) -> dict:
    """Terminate an explicit list of pids that have ALREADY been approved
    by loop.policy_gate. This tool trusts the caller's pid list completely
    and does no threshold or safety logic of its own — which is exactly
    why the harness never lets the model call this directly. The model
    only ever sees apply_pool_action's dry-run proposals; execute_pool_action
    is invoked by harness code after the gate runs, not by a model tool call."""
    return await db.terminate_by_pids(pids)


if __name__ == "__main__":
    mcp.run()