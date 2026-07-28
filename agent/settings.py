"""Load and validate configuration.

Central place that reads config.yaml + environment. The one hard rule enforced here is the
paper/live interlock: to reach ``mode == "live"`` you need BOTH ``account.mode: live`` in
the config AND ``IBKR_ALLOW_LIVE=1`` in the environment. Any other combination resolves to
paper, so a stray config edit or a stray env var can never flip you live on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class ConfigError(ValueError):
    """Raised when the configuration is missing required fields or is internally inconsistent."""


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float  # in account base currency
    max_position_weight_pct: float
    max_sector_weight_pct: float
    max_new_positions_per_review: int
    cash_buffer_pct: float
    stop_loss_pct: float
    correction_stop_loss_pct: float
    take_profit_pct: float


@dataclass(frozen=True)
class Settings:
    mode: str  # resolved effective mode: "paper" or "live"
    config_mode: str  # what config.yaml requested (before the env interlock)
    base_currency: str
    account_verify: dict[str, Any]  # non-sensitive identity markers for the verify check
    strategy_style: str
    asset_classes: tuple[str, ...]
    new_ideas_count: int
    risk: RiskLimits
    management: dict[str, Any]
    universe: dict[str, Any]
    schedule: dict[str, Any]
    recommend_skill_path: Path
    grader_skill_path: Path
    journal_db_path: Path
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _require(d: dict[str, Any], key: str, section: str) -> Any:
    if key not in d:
        raise ConfigError(f"config.yaml missing required key '{section}.{key}'")
    return d[key]


def _resolve_mode(config_mode: str) -> str:
    """Apply the two-switch live interlock. Returns the effective mode."""
    config_mode = (config_mode or "paper").strip().lower()
    if config_mode not in ("paper", "live"):
        raise ConfigError(f"account.mode must be 'paper' or 'live', got {config_mode!r}")
    env_allows_live = os.environ.get("IBKR_ALLOW_LIVE", "").strip() in ("1", "true", "yes")
    if config_mode == "live" and env_allows_live:
        return "live"
    return "paper"


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    path = Path(config_path or os.environ.get("IBKR_AGENT_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}

    account = _require(data, "account", "root")
    risk_raw = _require(data, "risk", "root")
    strategy = data.get("strategy", {})
    skills = data.get("skills", {})
    journal = data.get("journal", {})

    config_mode = str(account.get("mode", "paper"))
    effective_mode = _resolve_mode(config_mode)

    risk = RiskLimits(
        max_order_notional=float(
            risk_raw.get("max_order_notional", risk_raw.get("max_order_notional_usd"))
            if (risk_raw.get("max_order_notional") is not None
                or risk_raw.get("max_order_notional_usd") is not None)
            else _require(risk_raw, "max_order_notional", "risk")
        ),
        max_position_weight_pct=float(_require(risk_raw, "max_position_weight_pct", "risk")),
        max_sector_weight_pct=float(_require(risk_raw, "max_sector_weight_pct", "risk")),
        max_new_positions_per_review=int(risk_raw.get("max_new_positions_per_review", 3)),
        cash_buffer_pct=float(risk_raw.get("cash_buffer_pct", 5)),
        stop_loss_pct=float(risk_raw.get("stop_loss_pct", 8)),
        correction_stop_loss_pct=float(risk_raw.get("correction_stop_loss_pct", 3)),
        take_profit_pct=float(risk_raw.get("take_profit_pct", 22)),
    )

    def _resolve(p: str, default: str) -> Path:
        raw = str(p or default)
        rp = Path(raw)
        return rp if rp.is_absolute() else REPO_ROOT / rp

    schedule_raw = dict(data.get("schedule", {}))
    # Validate the cadence now so a bad frequency/time fails at load, not at run time.
    from agent.schedule import ScheduleError, resolve_schedule

    try:
        resolve_schedule(schedule_raw)
    except ScheduleError as exc:
        raise ConfigError(str(exc)) from exc

    return Settings(
        mode=effective_mode,
        config_mode=config_mode.lower(),
        base_currency=str(account.get("base_currency", "USD")),
        account_verify=dict(account.get("verify", {}) or {}),
        strategy_style=str(strategy.get("style", "blend")),
        asset_classes=tuple(strategy.get("asset_classes", ["stock", "etf"])),
        new_ideas_count=int(strategy.get("new_ideas_count", 20)),
        risk=risk,
        management=dict(data.get("management", {})),
        universe=dict(data.get("universe", {})),
        schedule=schedule_raw,
        recommend_skill_path=_resolve(
            skills.get("recommend_path", ""), "skills/can-slim-recommend"
        ),
        grader_skill_path=_resolve(skills.get("grader_path", ""), "skills/can-slim-grader"),
        journal_db_path=_resolve(journal.get("db_path", ""), "journal/trade_journal.db"),
        raw=data,
    )
