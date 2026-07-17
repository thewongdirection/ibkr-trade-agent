"""Claude Agent SDK runtime wiring.

Builds the agent that drives the portfolio review: it attaches the IBKR + FMP MCP
connectors, loads the CAN SLIM analysis skills, and installs a permission callback that is
the last line of defense against an unapproved live order.

The Agent SDK import is deferred and guarded so the rest of the package (settings,
guardrails, journal, tests) is usable without the SDK installed. Points that need a live
MCP session are marked ``TODO(connector)``.
"""

from __future__ import annotations

from pathlib import Path

from agent.settings import Settings, load_settings

# IBKR MCP tools the agent is permitted to call. Read + order-staging only; NO order
# confirmation/submit tool appears here, and the CAN SLIM skills are additionally
# restricted to the read-only subset (see analysis/canslim.py).
IBKR_READ_TOOLS = (
    "get_account_balances",
    "get_account_positions",
    "get_account_summary",
    "get_account_orders",
    "get_account_trades",
    "get_price_snapshot",
    "get_price_history",
    "search_contracts",
    "search_investment_topics",
    "get_theme_details",
    "get_company_themes",
    "get_option_parameters",
    "get_option_data",
    "get_watchlists",
    "get_watchlist",
)
# Staging only. Approval/execution is done by the user in IBKR, never by a tool call here.
IBKR_STAGE_TOOLS = ("create_order_instruction", "delete_order_instruction")

BLOCKED_TOOLS: tuple[str, ...] = ()  # reserved: any tool that would auto-execute a trade


def _read_system_prompt() -> str:
    return (Path(__file__).parent / "system_prompt.md").read_text()


def build_permission_callback(settings: Settings):
    """Return a permission callback for the Agent SDK.

    Enforces two invariants regardless of what the model tries:
      * Order staging is allowed, but the payload is checked by the risk layer first.
      * There is no path to an execute/confirm tool.
    """
    from risk.guardrails import check_order_instruction  # local import: no SDK dependency

    async def can_use_tool(tool_name: str, tool_input: dict, context=None):
        # Deny anything explicitly blocked.
        if tool_name in BLOCKED_TOOLS:
            return {"behavior": "deny", "message": f"{tool_name} is not permitted."}

        # Gate order staging through the risk checks.
        if tool_name == "create_order_instruction":
            verdict = check_order_instruction(tool_input, settings)
            if not verdict.ok:
                return {"behavior": "deny", "message": verdict.reason}
        return {"behavior": "allow", "updated_input": tool_input}

    return can_use_tool


def build_agent(settings: Settings | None = None):
    """Construct the Agent SDK client. Requires ``claude-agent-sdk`` installed.

    TODO(connector): the IBKR and FMP MCP servers are provided by the Claude environment.
    In a hosted session they are auto-discovered; for a standalone run, register them here
    with their stdio/SSE launch configs.
    """
    settings = settings or load_settings()
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    except ImportError as exc:  # pragma: no cover - exercised only without the SDK
        raise RuntimeError(
            "claude-agent-sdk is not installed. Run `pip install -e .` first."
        ) from exc

    allowed_tools = [
        *(f"mcp__Interactive_Brokers_IBKR__{t}" for t in IBKR_READ_TOOLS),
        *(f"mcp__Interactive_Brokers_IBKR__{t}" for t in IBKR_STAGE_TOOLS),
    ]

    options = ClaudeAgentOptions(
        system_prompt=_read_system_prompt(),
        allowed_tools=allowed_tools,
        permission_mode="default",
        can_use_tool=build_permission_callback(settings),
        # TODO(connector): mcp_servers={...} for standalone (non-hosted) runs.
        # TODO(skills): register settings.recommend_skill_path / grader_skill_path as skills.
        setting_sources=["project"],
    )
    return ClaudeSDKClient(options=options)


def mode_banner(settings: Settings) -> str:
    """A one-line human banner making the operating mode unmissable."""
    if settings.is_live:
        return "*** LIVE ACCOUNT *** orders staged for your approval only."
    banner = "[PAPER] paper account — orders staged for your approval only."
    if settings.config_mode == "live":
        banner += " (config requests live, but IBKR_ALLOW_LIVE is not set → paper)"
    return banner
