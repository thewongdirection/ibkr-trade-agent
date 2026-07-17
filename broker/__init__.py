"""Broker data-access layer: connection, account retrieval, and mode switching.

This package is the typed boundary between the IBKR MCP connector and the rest of the agent.
The connector is authenticated at the Claude-connector (OAuth) level, so there is no
username/password login to write here; "connecting" means verifying the connector is
reachable/authorized and identifying which account (and mode) we are operating on.
"""

from broker.client import (
    AccountMeta,
    AccountSummary,
    BrokerClient,
    CurrencyBalance,
    MCPBrokerClient,
    Order,
    Position,
    StaticBrokerClient,
    Trade,
)
from broker.session import ConnectionStatus, verify_connection

__all__ = [
    "AccountMeta",
    "AccountSummary",
    "BrokerClient",
    "CurrencyBalance",
    "MCPBrokerClient",
    "Order",
    "Position",
    "StaticBrokerClient",
    "Trade",
    "ConnectionStatus",
    "verify_connection",
]
