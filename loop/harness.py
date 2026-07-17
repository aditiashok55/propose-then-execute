"""
The actual loop: reason -> act -> observe -> verify -> repeat or stop.

This version uses Google's Gemini API (via google-genai) instead of
Claude. The control flow is identical to the Claude version — same
trigger/act/observe/verify/stop structure — only the model-calling and
tool-result plumbing changed, because Gemini's SDK shapes responses
differently than Anthropic's.

Guardrails (see README):
  - dry_run is decided HERE, by the harness, never by the model and never
    implicitly. Real terminations only happen with --live on the CLI.
  - hard cap on iterations so a confused agent can't loop forever.
  - every iteration is logged to transcript.json regardless of outcome.

Note: Gemini's Python SDK surface has been evolving quickly (there is a
newer "Interactions API" alongside the generate_content API used here).
This uses the generate_content + manual contents-list pattern because
it's the most stable, well-documented way to get full control over each
tool call — which we need for the dry_run guardrail below. If something
here doesn't match current docs, check https://ai.google.dev/gemini-api/docs/function-calling
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from loop.policy_gate import Proposal, evaluate
from loop.verifier import describe_goal, goal_met

load_dotenv()

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_ITERATIONS = 5

SYSTEM_PROMPT = """You are an on-call database reliability agent for a Postgres instance.

GOAL: {goal}

You have tools to inspect connection stats, inspect slow queries, and
apply a remediation action (terminating idle-in-transaction sessions).

Rules:
- Always check connection stats and slow queries before acting.
- Only use apply_pool_action when idle-in-transaction sessions are the
  clear cause of high utilization.
- Be conservative: prefer a higher threshold_seconds over a lower one
  unless utilization is critically high.
- After acting, you will be shown updated stats. Decide whether the goal
  is met or another action is needed.
""".format(goal=describe_goal())


# Tools the model is allowed to call directly. execute_pool_action is
# deliberately excluded — it is only ever invoked by harness code, after
# loop.policy_gate has approved specific pids. This isn't a prompt-level
# instruction the model could ignore or be injected around; the model
# never receives a function declaration for it, so it cannot call it.
MODEL_FACING_TOOLS = {"get_connection_stats", "get_slow_queries", "apply_pool_action"}


def _mcp_tools_to_gemini(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions into Gemini's function_declarations format.
    Filters to MODEL_FACING_TOOLS — see note above."""
    return [
        {
            "function_declarations": [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                }
                for t in mcp_tools
                if t.name in MODEL_FACING_TOOLS
            ]
        }
    ]


async def run_loop(live: bool):
    transcript = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "live": live,
        "model": MODEL,
        "goal": describe_goal(),
        "iterations": [],
    }
    history: list[dict] = []

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
    )

    # Picks up GEMINI_API_KEY (or GOOGLE_API_KEY) from the environment.
    client = genai.Client()
    stop_reason = "agent_stopped"

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            gemini_tools = _mcp_tools_to_gemini(mcp_tools)

            config = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=gemini_tools,
            )

            contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text="Check the current connection pool health and remediate if needed."
                        )
                    ],
                )
            ]

            for iteration in range(1, MAX_ITERATIONS + 1):
                iter_log = {"iteration": iteration, "tool_calls": []}

                response = client.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=config,
                )

                candidate_content = response.candidates[0].content
                contents.append(candidate_content)

                function_calls = [p.function_call for p in candidate_content.parts if p.function_call]
                text_parts = [p.text for p in candidate_content.parts if p.text]
                iter_log["agent_text"] = "\n".join(text_parts)

                if not function_calls:
                    iter_log["note"] = "No tool call this turn; agent considers it done or is stuck."
                    transcript["iterations"].append(iter_log)
                    stop_reason = "agent_stopped_no_tool_call"
                    break

                response_parts = []
                slow_queries_by_pid: dict[int, str] = {}

                for fc in function_calls:
                    args = dict(fc.args or {})

                    # apply_pool_action is now ALWAYS a proposal, regardless of
                    # --live. Real execution only ever happens through the
                    # gate below, never as a direct model tool call.
                    if fc.name == "apply_pool_action":
                        args["dry_run"] = True

                    result = await session.call_tool(fc.name, args)
                    result_text = "".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )

                    iter_log["tool_calls"].append(
                        {"name": fc.name, "input": args, "result": result_text}
                    )

                    if fc.name == "get_connection_stats":
                        try:
                            history.append(json.loads(result_text))
                        except json.JSONDecodeError:
                            pass

                    if fc.name == "get_slow_queries":
                        try:
                            for row in json.loads(result_text):
                                slow_queries_by_pid[row["pid"]] = row.get("query", "")
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass

                    final_result_text = result_text

                    if fc.name == "apply_pool_action":
                        try:
                            proposal_data = json.loads(result_text)
                        except json.JSONDecodeError:
                            proposal_data = {}

                        candidates = proposal_data.get("would_terminate", [])
                        proposals = [
                            Proposal(
                                pid=c["pid"],
                                idle_seconds=c["idle_seconds"],
                                query_text=slow_queries_by_pid.get(c["pid"]),
                            )
                            for c in candidates
                        ]
                        gate_decision = evaluate(proposals)

                        gate_log = {
                            "proposed_pids": [p.pid for p in proposals],
                            "approved_pids": gate_decision.approved_pids,
                            "rejected": gate_decision.rejected,
                            "executed": False,
                        }

                        if live and gate_decision.approved_pids:
                            # Harness calls execute_pool_action directly — this
                            # is NOT a model tool call. The model never sees
                            # this happen as its own turn.
                            exec_result = await session.call_tool(
                                "execute_pool_action",
                                {"pids": gate_decision.approved_pids},
                            )
                            exec_text = "".join(
                                c.text for c in exec_result.content if hasattr(c, "text")
                            )
                            gate_log["executed"] = True
                            gate_log["execution_result"] = exec_text
                            iter_log["tool_calls"].append(
                                {
                                    "name": "execute_pool_action",
                                    "input": {"pids": gate_decision.approved_pids},
                                    "result": exec_text,
                                    "called_by": "harness_policy_gate",
                                }
                            )

                        iter_log["policy_gate"] = gate_log

                        # What the MODEL sees back is the gate's decision, not
                        # raw termination access — it learns what happened,
                        # it doesn't get to make it happen.
                        final_result_text = json.dumps(
                            {
                                "proposal": proposal_data,
                                "policy_gate_decision": gate_log,
                            }
                        )

                    response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name, response={"result": final_result_text}
                        )
                    )

                contents.append(types.Content(role="user", parts=response_parts))

                met = goal_met(history)
                iter_log["goal_met_so_far"] = met
                transcript["iterations"].append(iter_log)

                if met:
                    stop_reason = "goal_met"
                    break
            else:
                stop_reason = "max_iterations_reached"

    transcript["stop_reason"] = stop_reason
    transcript["finished_at"] = datetime.now(timezone.utc).isoformat()

    with open("transcript.json", "w") as f:
        json.dump(transcript, f, indent=2, default=str)

    print(f"Done. stop_reason={stop_reason} -> see transcript.json")


def main():
    parser = argparse.ArgumentParser(description="Run the connection-pool remediation loop.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow real session terminations. Default is dry-run.",
    )
    args = parser.parse_args()
    asyncio.run(run_loop(live=args.live))


if __name__ == "__main__":
    main()