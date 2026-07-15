"""
The goal condition lives here, on its own, deliberately separated from
the loop control flow. This is the piece worth pointing to when explaining
"what makes this a loop and not just a script that runs once."

Goal: utilization_ratio < THRESHOLD, sustained for STREAK_REQUIRED
consecutive observations taken after at least one action has been applied.
"""

THRESHOLD = 0.8
STREAK_REQUIRED = 2


def goal_met(history: list[dict]) -> bool:
    """history is a list of {"utilization_ratio": float, ...} observations,
    most recent last. Returns True once the ratio has stayed below
    THRESHOLD for STREAK_REQUIRED consecutive observations."""
    if len(history) < STREAK_REQUIRED:
        return False

    recent = history[-STREAK_REQUIRED:]
    return all(
        obs.get("utilization_ratio") is not None and obs["utilization_ratio"] < THRESHOLD
        for obs in recent
    )


def describe_goal() -> str:
    return (
        f"utilization_ratio < {THRESHOLD}, sustained for {STREAK_REQUIRED} "
        f"consecutive checks after an action has been taken"
    )
