"""Connection ("login") verification and account-identity checks.

The IBKR MCP connector is authorized out-of-band (Claude connector OAuth), so there is no
password to handle here. This module answers two questions instead:

  1. *Are we connected?* — can we reach the connector and read the account.
  2. *Is this the RIGHT account (yours)?* — does the live account match the non-sensitive
     markers you recorded in ``config.yaml → account.verify``.

The connector exposes no account number, so identity is established by a **fingerprint** the
owner recognizes plus an optional **expected-marker** assertion. No credential ever touches
this code or the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.settings import Settings, load_settings
from broker.client import BrokerClient


@dataclass(frozen=True)
class ConnectionStatus:
    connected: bool
    mode: str                       # effective mode: paper | live
    base_currency: str
    is_funded: bool
    net_liquidation: float
    total_cash: float
    position_count: int
    # identity_verified: True/False when expected markers are set; None when not configured.
    identity_verified: bool | None
    fingerprint: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok_to_trade(self) -> bool:
        """Safe to proceed to (staging) trades: connected and identity not actively failing."""
        return self.connected and self.identity_verified is not False


def account_fingerprint(client: BrokerClient, trades_period: str = "DAYS_30") -> dict[str, Any]:
    """A human-recognizable, non-sensitive snapshot of the connected account.

    The connector masks the account number and owner name, so this fingerprint is the basis
    for identity: base currency, account inception date, funding, positions, and recent
    trades — signals only the true owner would recognize.
    """
    summary = client.account_summary()
    meta = client.account_meta()
    positions = client.positions()
    trades = client.trades(period=trades_period)
    return _fingerprint_from(summary, meta, positions, trades)


def _fingerprint_from(summary, meta, positions, trades) -> dict[str, Any]:
    """Build the fingerprint from already-fetched account data (no extra connector calls)."""
    return {
        "account_number": "masked by connector (not exposed)",
        "owner_name": "masked by connector (not exposed)",
        "base_currency": summary.currency or meta.base_currency,
        "account_inception": meta.inception_date,
        "last_update": meta.last_update,
        "net_liquidation": round(summary.net_liquidation, 2),
        "total_cash": round(summary.total_cash_value, 2),
        "buying_power": round(summary.buying_power, 2),
        "position_count": len(positions),
        "position_symbols": sorted({p.symbol for p in positions if p.symbol})[:15],
        "recent_trade_count": len(trades),
        "recent_trade_symbols": sorted({t.symbol for t in trades if t.symbol})[:15],
    }


def verify_connection(client: BrokerClient, settings: Settings) -> ConnectionStatus:
    """Ping the connector, build the fingerprint, and check identity markers if configured."""
    try:
        summary = client.account_summary()
        meta = client.account_meta()
        positions = client.positions()
        trades = client.trades(period="DAYS_30")
    except Exception as exc:  # pragma: no cover - connector/transport failure path
        return ConnectionStatus(
            connected=False, mode=settings.mode, base_currency="", is_funded=False,
            net_liquidation=0.0, total_cash=0.0, position_count=0, identity_verified=None,
            error=f"could not reach IBKR connector: {exc}",
        )

    fingerprint = _fingerprint_from(summary, meta, positions, trades)

    warnings: list[str] = []

    # Config declares a base currency; the live account reports its own. Flag a mismatch so
    # USD-denominated risk caps aren't silently applied to, say, an SGD account.
    if settings.base_currency and summary.currency and (
        settings.base_currency.upper() != summary.currency.upper()
    ):
        warnings.append(
            f"config base_currency={settings.base_currency} but the connected account is "
            f"{summary.currency}; risk caps are interpreted in {summary.currency}."
        )

    # Identity check against the non-sensitive expected markers.
    verify = settings.account_verify or {}
    expected_ccy = str(verify.get("expected_base_currency", "") or "").strip()
    label = str(verify.get("label", "") or "").strip()

    if not expected_ccy:
        identity_verified: bool | None = None
        warnings.append(
            "account identity not yet verified — set account.verify.expected_base_currency "
            f"(this account is {summary.currency or 'unknown'}) in config.yaml to lock it in."
        )
    else:
        identity_verified = expected_ccy.upper() == (summary.currency or "").upper()
        if not identity_verified:
            warnings.append(
                f"IDENTITY MISMATCH: expected a {expected_ccy} account"
                + (f" ('{label}')" if label else "")
                + f" but the connected account is {summary.currency}. Refusing to trade."
            )

    return ConnectionStatus(
        connected=True,
        mode=settings.mode,
        base_currency=summary.currency,
        is_funded=summary.is_funded,
        net_liquidation=summary.net_liquidation,
        total_cash=summary.total_cash_value,
        position_count=len(positions),
        identity_verified=identity_verified,
        fingerprint=fingerprint,
        warnings=tuple(warnings),
    )


def format_status(status: ConnectionStatus, settings: Settings) -> str:
    """Human-readable connection + identity report."""
    lines: list[str] = []
    if not status.connected:
        lines.append("IBKR connection: FAILED")
        lines.append(f"  {status.error}")
        return "\n".join(lines)

    mode_label = "LIVE" if status.mode == "live" else "PAPER"
    fp = status.fingerprint
    lines.append(f"IBKR connection: OK  [{mode_label}]")
    lines.append("  Account number: masked by connector (IBKR MCP never exposes it)")
    lines.append("  Owner name    : masked by connector (IBKR MCP never exposes it)")
    lines.append(f"  Base currency : {status.base_currency or 'unknown'}")
    if fp.get("account_inception"):
        lines.append(f"  Inception date: {fp['account_inception']}  (a signal only you'd know)")
    lines.append(f"  Net liquidation: {status.net_liquidation:,.2f} {status.base_currency}")
    lines.append(f"  Total cash    : {status.total_cash:,.2f} {status.base_currency}")
    lines.append(f"  Open positions: {status.position_count}")
    lines.append(f"  Funded        : {'yes' if status.is_funded else 'no (empty account)'}")

    if status.identity_verified is True:
        lines.append("  Identity      : VERIFIED (matches your configured markers)")
    elif status.identity_verified is False:
        lines.append("  Identity      : MISMATCH -- this is NOT your configured account")
    else:
        lines.append("  Identity      : not configured (see below to lock it in)")

    if status.warnings:
        lines.append("")
        lines.append("Notes:")
        for w in status.warnings:
            lines.append(f"  - {w}")

    if status.identity_verified is None:
        lines.append("")
        lines.append("To verify this is your account, confirm the fingerprint above matches")
        lines.append("your real IBKR account, then set in config.yaml:")
        lines.append(f"  account.verify.expected_base_currency: {status.base_currency}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: verify the IBKR connection and account identity.

        python -m broker.session
    """
    from agent.runtime import build_broker_client

    settings = load_settings()
    try:
        client = build_broker_client(settings)
        status = verify_connection(client, settings)
    except Exception as exc:  # noqa: BLE001 - surface any connector/transport error cleanly
        print("IBKR connection: FAILED")
        print(f"  {exc}")
        print(
            "\nThis check runs inside a Claude session with the IBKR connector attached "
            "(or a standalone Agent SDK MCP client bound in build_broker_client())."
        )
        return 2

    print(format_status(status, settings))
    return 0 if status.ok_to_trade else 1


if __name__ == "__main__":
    raise SystemExit(main())
