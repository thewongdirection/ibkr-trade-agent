"""Wiring to the CAN SLIM analysis skills.

The heavy lifting lives in the two Claude Skills (``can-slim-recommend`` and
``can-slim-grader``), which the agent invokes at runtime. This module is the typed boundary
between those skills and the trade agent: it locates the skills, exposes the small vocabulary
of verdicts/grades the workflow reasons over, and maps a grade to a management action.

The skills output rich HTML dashboards for humans; for the automated weekly loop the agent
is instructed (see ``agent/system_prompt.md``) to also return the structured summary this
module parses. ``TODO(connector)`` marks where the live skill invocation happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agent.settings import Settings

# Read-only IBKR tools the CAN SLIM skills are allowed to touch. The skills MUST NOT be
# granted order or account-mutation tools; this is the allow-list the runtime scopes them to.
CANSLIM_READONLY_TOOLS = (
    "search_contracts",
    "get_price_snapshot",
    "get_price_history",
    "search_investment_topics",
    "get_theme_details",
    "get_company_themes",
    "get_watchlists",
    "get_watchlist",
)


class Verdict(str, Enum):
    BUY_RANGE = "BUY-RANGE"
    WATCH = "WATCH"
    AVOID = "AVOID"


class ManagementAction(str, Enum):
    HOLD = "HOLD"
    TRIM = "TRIM"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Grade:
    """Structured result of grading one ticker with can-slim-grader."""

    symbol: str
    verdict: Verdict
    score_out_of_70: int              # C+A+N+S+L+I + M
    letters: dict[str, int]           # {"C": 8, "A": 7, ...}
    pivot: float | None = None        # buy point if actionable
    stop: float | None = None         # 7-8% loss-cutting stop
    summary: str = ""


@dataclass(frozen=True)
class Recommendation:
    """One idea from can-slim-recommend's ranked shortlist."""

    symbol: str
    company: str
    sector: str
    score_out_of_70: int
    price: float
    buy_point: float
    stop: float
    rs: float
    reason: str                       # CAN SLIM-only rationale


def skills_installed(settings: Settings) -> dict[str, bool]:
    """Report whether each analysis skill has been cloned into ./skills."""
    return {
        "can-slim-recommend": (settings.recommend_skill_path / "SKILL.md").exists(),
        "can-slim-grader": (settings.grader_skill_path / "SKILL.md").exists(),
    }


def require_skills(settings: Settings) -> None:
    """Raise a clear, actionable error if the skills aren't present."""
    status = skills_installed(settings)
    missing = [name for name, ok in status.items() if not ok]
    if missing:
        raise FileNotFoundError(
            "Missing CAN SLIM skill(s): "
            + ", ".join(missing)
            + ". Run scripts/setup_skills.sh to clone them into ./skills."
        )


def decide_management_action(
    grade: Grade,
    unrealized_pnl_pct: float,
    dte: int | None,
    settings: Settings,
) -> tuple[ManagementAction, str]:
    """Map a holding's grade + P&L to HOLD / TRIM / EXIT with a CAN SLIM-framed reason.

    Deterministic policy so the weekly review is reproducible; the agent still narrates it.
    """
    mgmt = settings.management
    hard_exit_loss = float(mgmt.get("hard_exit_on_loss_pct", settings.risk.stop_loss_pct))
    trim_gain = float(mgmt.get("trim_on_gain_pct", settings.risk.take_profit_pct))
    dte_warn = int(mgmt.get("option_dte_warn", 10))

    # Defense first: honor the stop regardless of grade.
    if unrealized_pnl_pct <= -abs(hard_exit_loss):
        return (
            ManagementAction.EXIT,
            f"down {unrealized_pnl_pct:.1f}% — through the {hard_exit_loss:.0f}% "
            "loss-cutting stop; cut the loss.",
        )

    # Option nearing expiry.
    if dte is not None and dte <= dte_warn:
        return (
            ManagementAction.EXIT,
            f"option {dte}d to expiry (<= {dte_warn}d) — roll or close before decay/pin risk.",
        )

    if grade.verdict is Verdict.AVOID:
        return (
            ManagementAction.EXIT,
            f"grade fell to AVOID ({grade.score_out_of_70}/70) — leadership/earnings "
            "deteriorated; exit the laggard.",
        )

    # Ring the register into strength.
    if unrealized_pnl_pct >= trim_gain:
        return (
            ManagementAction.TRIM,
            f"up {unrealized_pnl_pct:.1f}% (>= {trim_gain:.0f}%) — take a partial gain, "
            "let the leader run.",
        )

    if grade.verdict is Verdict.WATCH:
        return (
            ManagementAction.HOLD,
            f"WATCH ({grade.score_out_of_70}/70) — still a leader but no fresh buy point; "
            "hold, don't add.",
        )

    return (
        ManagementAction.HOLD,
        f"BUY-RANGE/holding strength ({grade.score_out_of_70}/70) — thesis intact.",
    )


def skill_invocation_plan(settings: Settings) -> dict[str, object]:
    """Describe how the agent should invoke the skills this run (for logging/telemetry).

    TODO(connector): the actual invocation is performed by the Agent SDK loop, which loads
    each SKILL.md and lets the model drive it with the read-only tool set above. This returns
    the plan the workflow logs so a run is auditable.
    """
    return {
        "recommend": {
            "path": str(settings.recommend_skill_path),
            "count": settings.new_ideas_count,
            "universe": settings.universe,
            "readonly_tools": list(CANSLIM_READONLY_TOOLS),
        },
        "grader": {
            "path": str(settings.grader_skill_path),
            "readonly_tools": list(CANSLIM_READONLY_TOOLS),
        },
    }
