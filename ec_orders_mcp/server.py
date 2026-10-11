"""MCP server exposing the order store as read-only tools.

Run over stdio (for Claude Desktop / Claude Code):
    python -m ec_orders_mcp
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .data import build
from .store import OrderStore, QueryRejected

INSTRUCTIONS = """\
Read-only access to an online shop's order data (synthetic).
Start with describe_schema if you need table or column details.
Prefer the fixed tools (search_orders, get_order, sales_summary, top_products);
use run_sql only for questions they cannot answer. Personal data is masked or
unavailable by design: do not try to work around it.
Dates are ISO strings (YYYY-MM-DD). Revenue counts completed orders only.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def create_server(db_path: str | Path | None = None, audit_log: str | Path | None = None) -> MCPServer:
    db_path = Path(db_path or os.environ.get("EC_MCP_DB", "data/orders.db"))
    if not db_path.exists():
        build(db_path)
    store = OrderStore(db_path, audit_log or os.environ.get("EC_MCP_AUDIT_LOG"))
    mcp = MCPServer("ec-orders", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ_ONLY)
    def describe_schema() -> dict:
        """List tables and columns, with notes on how revenue and dates are defined."""
        return store.describe_schema()

    @mcp.tool(annotations=READ_ONLY)
    def search_orders(
        status: str | None = None,
        channel: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        customer_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find orders, newest first.

        Args:
            status: completed, shipped, cancelled or returned.
            channel: marketplace_a, marketplace_b, marketplace_c or own_store.
            date_from: first day to include (YYYY-MM-DD).
            date_to: last day to include (YYYY-MM-DD).
            customer_id: e.g. C00012.
            limit: 1-100 rows.
        """
        return store.search_orders(status, channel, date_from, date_to, customer_id, limit)

    @mcp.tool(annotations=READ_ONLY)
    def get_order(order_id: str) -> dict:
        """Get one order with its lines and a masked customer profile, e.g. order_id='ORD-10042'."""
        order = store.get_order(order_id)
        if order is None:
            raise ToolError(f"Order {order_id} not found.")
        return order

    @mcp.tool(annotations=READ_ONLY)
    def sales_summary(date_from: str, date_to: str, group_by: str = "month") -> list[dict]:
        """Revenue, order count and average order value for completed orders.

        Args:
            date_from: first day to include (YYYY-MM-DD).
            date_to: last day to include (YYYY-MM-DD).
            group_by: day, week, month, channel or category.
        """
        try:
            return store.sales_summary(date_from, date_to, group_by)
        except QueryRejected as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ_ONLY)
    def top_products(
        date_from: str | None = None, date_to: str | None = None, metric: str = "revenue", limit: int = 10
    ) -> list[dict]:
        """Best-selling products for completed orders, ranked by 'revenue' or 'quantity'."""
        try:
            return store.top_products(date_from, date_to, metric, limit)
        except QueryRejected as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ_ONLY)
    def run_sql(query: str, max_rows: int = 200) -> dict:
        """Run one read-only SELECT against the database (SQLite dialect).

        Use only when the other tools cannot answer. Writes, PRAGMA, attached
        databases and personal-data columns are rejected; results are capped at
        200 rows and 2 seconds.
        """
        try:
            return store.run_sql(query, max_rows).as_dict()
        except QueryRejected as exc:
            raise ToolError(str(exc)) from None

    @mcp.resource("schema://tables", mime_type="application/json", description="Database schema and definitions")
    def schema_resource() -> dict:
        return store.describe_schema()

    @mcp.prompt()
    def monthly_report(month: str) -> str:
        """Ask for a short sales report for a month (YYYY-MM)."""
        return (
            f"Write a short sales report for {month}. Use sales_summary grouped by channel and by category, "
            "and top_products for the month. Lead with the three numbers a manager needs, then one paragraph "
            "on what changed versus the previous month, then one suggested action. Do not include personal data."
        )

    return mcp


def main() -> None:
    create_server().run("stdio")
